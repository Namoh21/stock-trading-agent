"""
db.py — Database and encryption layer for the Trading Agent.

All persistent state lives in a single SQLite file (trading_agent.db).
Sensitive config values (api_key) are encrypted with Fernet symmetric
encryption. The encryption key lives in .keyfile (chmod 600) and never
touches the database in plaintext.

Tables
------
config              key/value settings; sensitive values stored encrypted
portfolio_snapshots one row per agent run, JSON-encoded positions
trades              every buy/sell attempt with outcome
run_logs            structured log lines, queryable by run_id / level
stock_scores        scored universe for each run (top 50 stored)
schedule_config     scheduled run times (HH:MM, 24-h, local time)
"""

import os
import stat
import json
import sqlite3
from pathlib import Path
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "trading_agent.db"
KEY_FILE = BASE_DIR / ".keyfile"

# ── Encryption ────────────────────────────────────────────────────────────────

def _load_or_create_fernet() -> Fernet:
    """Load existing key or generate one on first run."""
    if KEY_FILE.exists():
        raw = KEY_FILE.read_bytes().strip()
    else:
        raw = Fernet.generate_key()
        KEY_FILE.write_bytes(raw)
        try:
            os.chmod(KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except Exception:
            pass  # Windows doesn't support POSIX chmod; acceptable
        print(f"[db] Encryption key created at {KEY_FILE}")
        print("     WARNING: Back this file up -- losing it makes stored secrets unrecoverable.")
    return Fernet(raw)


_fernet: Fernet = _load_or_create_fernet()


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Decryption failed — keyfile may have changed or data is corrupt.")


# ── Connection ─────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                encrypted   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS schedule_config (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time    TEXT NOT NULL UNIQUE,   -- HH:MM 24-h local time
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT NOT NULL,
                captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
                cash            REAL,
                invested        REAL,
                total_value     REAL,
                pnl             REAL,
                positions_json  TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                executed_at TEXT NOT NULL DEFAULT (datetime('now')),
                symbol      TEXT NOT NULL,
                side        TEXT NOT NULL,
                shares      REAL NOT NULL,
                price       REAL,
                total       REAL,
                score       INTEGER,
                reason      TEXT,
                status      TEXT NOT NULL DEFAULT 'placed',
                error_msg   TEXT
            );

            CREATE TABLE IF NOT EXISTS run_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id     TEXT NOT NULL,
                logged_at  TEXT NOT NULL DEFAULT (datetime('now')),
                level      TEXT NOT NULL,
                message    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_scores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                scored_at   TEXT NOT NULL DEFAULT (datetime('now')),
                symbol      TEXT NOT NULL,
                price       REAL,
                change_pct  REAL,
                score       INTEGER,
                upside      REAL,
                rank        INTEGER
            );
        """)


# ── Config helpers ─────────────────────────────────────────────────────────────

_SENSITIVE_KEYS = {"api_key"}

_DEFAULTS: dict[str, str] = {
    "base_url":  "https://stocks.namoh.net",
    "game_id":   "1",
    "username":  "",
    "api_key":   "",
    "timezone":  "America/New_York",
}


def config_set(key: str, value: str) -> None:
    is_sensitive = key in _SENSITIVE_KEYS
    stored = encrypt(value) if is_sensitive else value
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO config (key, value, encrypted, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                   value      = excluded.value,
                   encrypted  = excluded.encrypted,
                   updated_at = excluded.updated_at""",
            (key, stored, 1 if is_sensitive else 0),
        )


def config_get(key: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value, encrypted FROM config WHERE key=?", (key,)).fetchone()
    if row is None:
        return _DEFAULTS.get(key, "")
    return decrypt(row["value"]) if row["encrypted"] else row["value"]


def config_get_all() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value, encrypted FROM config").fetchall()
    result = dict(_DEFAULTS)
    for row in rows:
        if row["encrypted"]:
            result[row["key"]] = "********"
        else:
            result[row["key"]] = row["value"]
    return result


# ── Schedule helpers ───────────────────────────────────────────────────────────

def schedule_add(run_time: str) -> None:
    """Add a HH:MM run time. Duplicate times are ignored."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_config (run_time) VALUES (?)", (run_time,)
        )


def schedule_remove(run_time: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM schedule_config WHERE run_time=?", (run_time,))


def schedule_list() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT run_time FROM schedule_config WHERE enabled=1 ORDER BY run_time"
        ).fetchall()
    return [r["run_time"] for r in rows]


# ── Portfolio snapshot ─────────────────────────────────────────────────────────

def save_portfolio_snapshot(
    run_id: str,
    cash: float,
    invested: float,
    pnl: float,
    positions: list[dict],
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_snapshots
               (run_id, cash, invested, total_value, pnl, positions_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, cash, invested, cash + invested, pnl, json.dumps(positions)),
        )


def get_latest_snapshot() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM portfolio_snapshots ORDER BY captured_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["positions"] = json.loads(d["positions_json"] or "[]")
    return d


# ── Trade logging ──────────────────────────────────────────────────────────────

def log_trade(
    run_id: str,
    symbol: str,
    side: str,
    shares: float,
    price: float,
    total: float,
    score: int | None,
    reason: str,
    status: str = "placed",
    error_msg: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (run_id, symbol, side, shares, price, total, score, reason, status, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, symbol, side, shares, price, total, score, reason, status, error_msg),
        )


def get_trade_history(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT executed_at, symbol, side, shares, price, total, score, reason, status, error_msg
               FROM trades ORDER BY executed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Run log ────────────────────────────────────────────────────────────────────

def log_run(run_id: str, level: str, message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO run_logs (run_id, level, message) VALUES (?, ?, ?)",
            (run_id, level, message),
        )


def get_run_logs(tail: int = 100, level: str | None = None) -> list[dict]:
    query = "SELECT logged_at, level, run_id, message FROM run_logs"
    params: list = []
    if level:
        query += " WHERE level=?"
        params.append(level.upper())
    query += " ORDER BY logged_at DESC LIMIT ?"
    params.append(tail)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── Stock scores ───────────────────────────────────────────────────────────────

def save_stock_scores(run_id: str, scored: list[dict]) -> None:
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO stock_scores
               (run_id, symbol, price, change_pct, score, upside, rank)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, s["ticker"], s["price"], s["chg"], s["score"], s["upside"], i + 1)
                for i, s in enumerate(scored[:50])
            ],
        )
