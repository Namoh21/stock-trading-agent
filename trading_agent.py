#!/usr/bin/env python3
"""
Trading Agent — Raspberry Pi Edition
All configuration and history stored in SQLite. No cleartext config files.

First-time setup:
  python trading_agent.py config set-api-key
  python trading_agent.py config set game_id 1
  python trading_agent.py config set username myname
  python trading_agent.py config set base_url https://stocks.namoh.net

Schedule management:
  python trading_agent.py schedule add 09:35
  python trading_agent.py schedule add 13:00
  python trading_agent.py schedule list
  python trading_agent.py schedule remove 13:00
  python trading_agent.py schedule run          # start the scheduler daemon

One-shot run:
  python trading_agent.py run

History & diagnostics:
  python trading_agent.py logs [--tail 100] [--level ERROR]
  python trading_agent.py history [--limit 50]
  python trading_agent.py portfolio
  python trading_agent.py config show
"""

import sys

# Some environments (notably systemd services with no locale set) leave
# stdout/stderr in a "latin-1" or ASCII encoding that can't represent the
# "…" used throughout this file's log messages, crashing with
# UnicodeEncodeError on the first log line. Force UTF-8 unconditionally.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import math
import uuid
import signal
import getpass
import logging
import argparse
import time as _time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

import db

# -- Strategy constants (tunable via code; future: config table) ----------------
BUDGET                   = 10_000
MAX_POSITIONS            = 8
MAX_PER_POS              = 2_000
MIN_SCORE                = 60
ROTATION_THRESHOLD       = 20
MIN_HOLD_GAIN            = -5.0
ROTATION_GAP_PROFITABLE  = 30

FALLBACK_TICKERS = [
    # ── Mega-cap tech ──────────────────────────────────────────────────────────
    "AAPL","MSFT","NVDA","GOOGL","GOOG","META","AMZN","TSLA","AVGO","ORCL",
    # ── Semiconductors ─────────────────────────────────────────────────────────
    "AMD","INTC","QCOM","ARM","MU","AMAT","MRVL","SMCI","KLAC","LRCX","ASML",
    "TXN","ADI","MCHP","ON","SWKS","MPWR","WOLF","NXPI",
    # ── Software & cloud ───────────────────────────────────────────────────────
    "CRM","NOW","ADBE","INTU","WDAY","TEAM","ZM","DDOG","SNOW","PLTR",
    "NET","CRWD","ZS","MDB","HUBS","GTLB","BILL","DOCN","ESTC","CFLT",
    # ── Fintech & payments ─────────────────────────────────────────────────────
    "V","MA","PYPL","SQ","COIN","HOOD","AFRM","SOFI","NU","STNE",
    # ── Big banks & financials ─────────────────────────────────────────────────
    "JPM","GS","BAC","MS","C","WFC","BLK","SCHW","AXP","SPGI","MCO",
    # ── Healthcare & pharma ────────────────────────────────────────────────────
    "LLY","NVO","ABBV","JNJ","MRK","AMGN","GILD","REGN","VRTX","BSX",
    "BMY","PFE","MRNA","BNTX","DXCM","ISRG","EW","ZBH","HCA",
    # ── Consumer ──────────────────────────────────────────────────────────────
    "COST","WMT","AMZN","HD","NKE","SBUX","MCD","PEP","KO","PM",
    "LULU","CMG","YUM","DPZ","ROST","TJX","LOW","TGT",
    # ── Media & streaming ──────────────────────────────────────────────────────
    "NFLX","DIS","SPOT","PARA","WBD","TTWO","EA","RBLX","U",
    # ── Travel & mobility ─────────────────────────────────────────────────────
    "UBER","LYFT","ABNB","BKNG","EXPE","MAR","HLT","DAL","UAL","AAL",
    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM","CVX","COP","OXY","SLB","HAL","MPC","PSX","VLO","EOG","PXD",
    # ── ETFs — NASDAQ-listed only (NYSE Arca / PCX not supported by platform) ──
    # QQQ = NASDAQ. SPY/IWM/DIA/XL*/ARKK are all NYSE Arca (PCX) — excluded.
    "QQQ",
    # ── Space, EV & emerging tech ──────────────────────────────────────────────
    "RKLB","ASTS","LUNR","ACHR","JOBY","LILM","IONQ","RGTI","QUBT","QBTS",
    "RIVN","LCID","NIO","LI","XPEV","CHPT","BLNK",
    # ── AI infrastructure ─────────────────────────────────────────────────────
    "SMCI","DELL","HPE","PSTG","NTAP","BBAI","SOUN","UPST","AI","CIEN",
]

# ── Precious metals universe ────────────────────────────────────────────────────
# Scoring thresholds for ETFs are scaled down so a 2% gold day ranks like a 5% stock day.
#
# Exchange note: most physical metal ETFs (GLD, SLV, IAU, etc.) trade on NYSE Arca (PCX)
# which this platform does NOT support. They will auto-block on first order attempt.
# Miners below (NEM, GOLD, AEM…) trade on NYSE/NASDAQ and work fine.
# OUNZ (VanEck) is NYSE-listed — the one physical ETF that should work.
METALS_ETFS: set[str] = {
    "OUNZ", # VanEck Merk Gold — NYSE listed (others are PCX, will auto-block)
    "GLD",  # SPDR Gold — NYSE Arca (PCX) — will auto-block on first attempt
    "IAU",  # iShares Gold — NYSE Arca (PCX)
    "GLDM", # SPDR Gold Mini — NYSE Arca (PCX)
    "SLV",  # iShares Silver — NYSE Arca (PCX)
    "PPLT", # Aberdeen Platinum — NYSE Arca (PCX)
    "PALL", # Aberdeen Palladium — NYSE Arca (PCX)
}

# Miners are leveraged to metal prices and move 2–5 % — scored like volatile stocks.
METALS_MINERS: set[str] = {
    "GDX",  # VanEck Gold Miners ETF
    "GDXJ", # VanEck Junior Gold Miners ETF
    "NEM",  # Newmont — world's largest gold miner
    "GOLD", # Barrick Gold
    "AEM",  # Agnico Eagle
    "WPM",  # Wheaton Precious Metals (streaming)
    "FNV",  # Franco-Nevada (streaming)
    "RGLD", # Royal Gold (streaming)
    "KGC",  # Kinross Gold
    "AGI",  # Alamos Gold
    "BTG",  # B2Gold
    "SIL",  # Global X Silver Miners ETF
    "PAAS", # Pan American Silver
    "HL",   # Hecla Mining
    "CDE",  # Coeur Mining
}

METALS_TICKERS: set[str] = METALS_ETFS | METALS_MINERS

# -- Logging --------------------------------------------------------------------
# Console-only; structured records go to the DB via DBHandler.

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)
logger.addHandler(_console)

_run_id: str = ""   # set at the start of each run


class DBHandler(logging.Handler):
    """Writes log records into run_logs table."""
    def emit(self, record: logging.LogRecord) -> None:
        if _run_id:
            try:
                db.log_run(_run_id, record.levelname, self.format(record))
            except Exception:
                pass


_db_handler = DBHandler()
_db_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_db_handler)


# -- HTTP session ---------------------------------------------------------------

def _make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    })
    return s


def _get(session: requests.Session, base_url: str, path: str) -> dict:
    resp = session.get(base_url + path, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(session: requests.Session, base_url: str, path: str, body: dict) -> dict:
    resp = session.post(base_url + path, json=body, timeout=15)
    logger.info("  -> %s %s", resp.status_code, resp.text[:200])
    resp.raise_for_status()
    return resp.json()


def _place_order(
    session: requests.Session,
    base_url: str,
    game_id: int,
    symbol: str,
    side: str,
    shares: float,
) -> dict:
    body = {"symbol": symbol, "type": side, "order_type": "market", "shares": shares}
    logger.info("  ORDER -> symbol=%s type=%s order_type=market shares=%g", symbol, side, shares)
    return _post(session, base_url, f"/api/games/{game_id}/orders", body)


# -- Portfolio helpers ---------------------------------------------------------

def _cash(p: dict) -> float:
    for k in ("available_cash", "cash_balance", "cash", "balance"):
        if k in p:
            return float(p[k])
    return float(BUDGET)


def _positions(p: dict) -> list[dict]:
    for k in ("holdings", "positions", "stocks"):
        if k in p and isinstance(p[k], list):
            return p[k]
    return []


def _pval(pos: dict) -> float:
    qty  = float(pos.get("quantity") or pos.get("shares") or 0)
    cost = float(
        pos.get("current_price") or pos.get("price") or
        pos.get("avg_price") or pos.get("cost_basis") or 0
    )
    for k in ("current_value", "value", "market_value"):
        if k in pos:
            return float(pos[k])
    return qty * cost


def _sym(pos: dict) -> str:
    return (pos.get("symbol") or pos.get("ticker") or pos.get("stock") or "").upper()


# -- Scoring -------------------------------------------------------------------

def _score(quote: dict, ticker: str) -> dict:
    price = float(
        quote.get("price") or quote.get("last") or quote.get("close") or
        quote.get("current_price") or quote.get("regularMarketPrice") or 0
    )
    chg = float(
        quote.get("changePercent") or quote.get("change_percent") or
        quote.get("changesPercentage") or quote.get("regularMarketChangePercent") or
        quote.get("percent_change") or quote.get("dp") or 0
    )
    h52 = float(
        quote.get("week52High") or quote.get("high_52_week") or
        quote.get("yearHigh") or quote.get("52WeekHigh") or price * 1.35
    )
    l52 = float(
        quote.get("week52Low") or quote.get("low_52_week") or
        quote.get("yearLow") or quote.get("52WeekLow") or price * 0.65
    )
    vol  = float(quote.get("volume") or quote.get("vol") or 0)
    avgv = float(
        quote.get("avgVolume") or quote.get("average_volume") or
        quote.get("avg_volume") or vol
    )

    # Metal ETFs track spot price directly and move much less than equities.
    # Scale momentum thresholds down by 0.4x so a +2 % gold day scores like
    # a +5 % stock day, keeping them competitive in the ranking.
    is_metal_etf = ticker in METALS_ETFS
    t = 0.4 if is_metal_etf else 1.0   # threshold scale factor

    sc = 50
    if   chg >  5*t: sc += 25
    elif chg >  3*t: sc += 18
    elif chg >  1*t: sc += 10
    elif chg >    0: sc +=  4
    elif chg < -5*t: sc -= 25
    elif chg < -3*t: sc -= 18
    elif chg < -1*t: sc -= 10
    elif chg <    0: sc -=  4

    rng = h52 - l52
    if rng > 0:
        pos = (price - l52) / rng
        if   pos > 0.85: sc += 20
        elif pos > 0.65: sc += 12
        elif pos > 0.45: sc +=  5
        elif pos < 0.20: sc -= 12
        elif pos < 0.35: sc -=  5

    if avgv > 0 and vol > avgv * 1.5:
        sc += 8

    upside = (h52 - price) / price * 100 if h52 > price else 0.0

    # Tag the asset class for display and reporting
    if ticker in METALS_ETFS:
        asset_class = "metal_etf"
    elif ticker in METALS_MINERS:
        asset_class = "metal_miner"
    else:
        asset_class = "equity"

    return {
        "ticker":      ticker,
        "price":       price,
        "chg":         chg,
        "score":       min(100, max(0, round(sc))),
        "upside":      upside,
        "asset_class": asset_class,
    }


_FALLBACK_UPSIDE = 35.0  # what price*1.35 produces — signals no real 52w data

def _composite(s: dict) -> float:
    # If upside is exactly the fallback value the API returned no 52w high.
    # Discount it heavily so ranking is driven by momentum score alone.
    upside = s["upside"]
    upside_weight = 0.05 if abs(upside - _FALLBACK_UPSIDE) < 0.5 else 0.4
    return s["score"] * (1 - upside_weight) + upside * upside_weight


# -- Core agent run -------------------------------------------------------------

def run_agent() -> None:
    global _run_id
    _run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

    # Load config from DB
    api_key  = db.config_get("api_key")
    base_url = db.config_get("base_url")
    game_id  = int(db.config_get("game_id") or 1)
    username = db.config_get("username")

    if not api_key:
        logger.error("API key not set. Run: python trading_agent.py config set-api-key")
        return

    session = _make_session(api_key)
    user_label = f" ({username})" if username else ""
    logger.info("======== TRADING AGENT run_id=%s game=%d%s ========", _run_id, game_id, user_label)

    # -- 1. Portfolio -----------------------------------------------------------
    logger.info("Fetching portfolio…")
    raw       = _get(session, base_url, f"/api/games/{game_id}/portfolio")
    cash      = _cash(raw)
    positions = _positions(raw)
    invested  = sum(_pval(p) for p in positions)
    pnl       = (cash + invested) - BUDGET
    logger.info(
        "Cash: $%.2f | Invested: $%.2f | Positions: %d | P&L: %+.2f",
        cash, invested, len(positions), pnl,
    )
    db.save_portfolio_snapshot(_run_id, cash, invested, pnl, positions)

    # -- 2. Discover tickers ----------------------------------------------------
    # Check whether the platform has a stock-list endpoint. Once we confirm it
    # doesn't (all endpoints return empty bodies), cache that in the DB so we
    # skip the 5 wasted HTTP calls on every subsequent run.
    tickers: list[str] = []

    API_HAS_STOCK_LIST_KEY = "api_has_stock_list"
    api_has_list = db.config_get(API_HAS_STOCK_LIST_KEY)  # "1", "0", or ""

    if api_has_list != "0":  # probe unless we've confirmed it doesn't exist
        logger.info("Probing API for stock list…")
        for ep in [
            "/api/stocks",
            "/api/stocks/list",
            f"/api/games/{game_id}/stocks",
            f"/api/games/{game_id}/available_stocks",
            "/api/market/stocks",
        ]:
            try:
                resp = session.get(base_url + ep, timeout=15)
                if not resp.ok or not resp.text.strip():
                    continue                      # empty body or HTTP error — skip quietly
                data = resp.json()
                arr = (
                    data if isinstance(data, list) else
                    data.get("stocks") or data.get("symbols") or
                    data.get("tickers") or data.get("data") or []
                )
                if arr:
                    tickers = [
                        (x if isinstance(x, str) else
                         x.get("symbol") or x.get("ticker") or x.get("stock") or "")
                        for x in arr
                    ]
                    tickers = [t for t in tickers if t]
                    logger.info("API stock list found: %d tickers from %s", len(tickers), ep)
                    db.config_set(API_HAS_STOCK_LIST_KEY, "1")
                    break
            except Exception:
                pass

        if not tickers and api_has_list == "":
            # First time we've confirmed the API has no stock list — cache it
            logger.info("API has no stock list endpoint — using built-in ticker list from now on")
            db.config_set(API_HAS_STOCK_LIST_KEY, "0")

    if not tickers:
        logger.info("Using built-in ticker list (%d tickers)", len(FALLBACK_TICKERS))
        tickers = list(FALLBACK_TICKERS)

    # Append precious metals tickers if enabled in config
    trade_metals = db.config_get("trade_metals") != "0"
    if trade_metals:
        metals_to_add = METALS_TICKERS - set(t.upper() for t in tickers)
        tickers = list(tickers) + sorted(metals_to_add)
        logger.info("Precious metals enabled — added %d metals tickers", len(metals_to_add))
    else:
        logger.info("Precious metals disabled — skipping metals tickers")

    # Remove permanently blocklisted tickers (e.g. wrong exchange, delisted)
    blocked = db.blocklist_get()
    if blocked:
        before = len(tickers)
        tickers = [t for t in tickers if t.upper() not in blocked]
        logger.info("Blocklist: skipping %d ticker(s) — %s", before - len(tickers), ", ".join(sorted(blocked)))

    # -- 3. Quote & score -------------------------------------------------------
    logger.info("Quoting %d tickers (stocks + metals)…", len(tickers))
    scored: list[dict] = []
    for tk in tickers:
        tk = tk.upper().strip()
        if not tk:
            continue
        try:
            q = _get(session, base_url, f"/api/stocks/quote/{tk}")
            s = _score(q, tk)
            if s["price"] > 0:
                scored.append(s)
        except Exception:
            pass

    scored.sort(key=_composite, reverse=True)
    db.save_stock_scores(_run_id, scored)

    metals_scored = [s for s in scored if s["asset_class"] in ("metal_etf", "metal_miner")]
    equities_scored = [s for s in scored if s["asset_class"] == "equity"]
    top5 = ", ".join(f"{s['ticker']}({s['score']})" for s in scored[:5])
    logger.info(
        "Scored %d tickers (%d equities, %d metals). Top 5: %s",
        len(scored), len(equities_scored), len(metals_scored), top5,
    )

    if metals_scored:
        logger.info("-- Top metals --")
        for s in metals_scored[:8]:
            label = "ETF  " if s["asset_class"] == "metal_etf" else "Miner"
            logger.info(
                "  %-6s [%s] $%8.2f  day %+5.1f%%  score %3d  upside %4.0f%%",
                s["ticker"], label, s["price"], s["chg"], s["score"], s["upside"],
            )

    logger.info("-- Top 20 ranked --")
    for i, s in enumerate(scored[:20], 1):
        logger.info(
            "  %2d. %-6s $%8.2f  day %+5.1f%%  score %3d  upside %4.0f%%",
            i, s["ticker"], s["price"], s["chg"], s["score"], s["upside"],
        )

    # -- 4. Sell analysis -------------------------------------------------------
    logger.info("-- SELL ANALYSIS --")
    cur_tickers = [_sym(p) for p in positions]

    best_opps = [
        s for s in scored
        if s["score"] >= MIN_SCORE and s["price"] > 0 and s["ticker"] not in cur_tickers
    ]
    top_opp = best_opps[0] if best_opps else None
    if top_opp:
        logger.info(
            "[TARGET] Best external opportunity: %s score=%d upside=%.0f%%",
            top_opp["ticker"], top_opp["score"], top_opp["upside"],
        )

    sell_list: list[dict] = []
    for pos in positions:
        sym  = _sym(pos)
        cost = float(
            pos.get("cost_basis") or pos.get("avg_price") or
            pos.get("average_price") or pos.get("cost") or 0
        )
        curr = float(pos.get("current_price") or pos.get("price") or cost)
        qty  = float(pos.get("quantity") or pos.get("shares") or 0)
        pct  = (curr - cost) / cost * 100 if cost else 0
        sc   = next((s for s in scored if s["ticker"] == sym), None)
        conf = sc["score"] if sc else 35
        reason = None

        if   conf < 40:               reason = f"score {conf}<40"
        elif pct >= 30 and conf < 75: reason = f"+{pct:.0f}% -> take profits"
        elif pct <= -15:              reason = f"{pct:.0f}% -> cut loss"

        if not reason and top_opp:
            gap      = top_opp["score"] - conf
            required = ROTATION_GAP_PROFITABLE if pct >= MIN_HOLD_GAIN else ROTATION_THRESHOLD
            if gap >= required:
                reason = f"rotate -> {top_opp['ticker']}({top_opp['score']}) | gap={gap}"
                logger.info(
                    "[ROTATE] ROTATE %s(%d) -> %s(%d): gap %d ≥ %d",
                    sym, conf, top_opp["ticker"], top_opp["score"], gap, required,
                )

        if reason:
            sell_list.append({"sym": sym, "reason": reason, "pos": pos, "qty": qty, "conf": conf, "price": curr})
            logger.warning("[SELL] SELL %s: %s", sym, reason)
        else:
            logger.info("[OK] HOLD %s: score %d, %+.1f%%", sym, conf, pct)

    avail_cash = cash
    for item in sell_list:
        if item["qty"] <= 0:
            continue
        logger.info("Selling %s ×%g…", item["sym"], item["qty"])
        total = round(item["qty"] * item["price"], 2)
        try:
            _place_order(session, base_url, game_id, item["sym"], "sell", item["qty"])
            logger.info("OK Sold %s", item["sym"])
            avail_cash += _pval(item["pos"])
            db.log_trade(
                _run_id, item["sym"], "sell", item["qty"],
                item["price"], total, item["conf"], item["reason"], "placed",
            )
        except Exception as e:
            logger.error("✗ Sell failed %s: %s", item["sym"], e)
            db.log_trade(
                _run_id, item["sym"], "sell", item["qty"],
                item["price"], total, item["conf"], item["reason"], "failed", str(e),
            )

    # -- 5. Buy analysis --------------------------------------------------------
    logger.info("-- BUY ANALYSIS --")
    sold_tickers = {item["sym"] for item in sell_list}
    existing     = [t for t in cur_tickers if t not in sold_tickers]
    slots        = max(0, MAX_POSITIONS - len(existing))

    rotate_targets: list[dict] = []
    for item in sell_list:
        if "rotate ->" in item["reason"]:
            tk = item["reason"].split("->")[1].split("(")[0].strip()
            match = next((s for s in scored if s["ticker"] == tk), None)
            if match:
                rotate_targets.append(match)

    general = [
        s for s in scored
        if (s["score"] >= MIN_SCORE and s["price"] > 0 and s["price"] <= avail_cash
            and s["ticker"] not in existing
            and not any(r["ticker"] == s["ticker"] for r in rotate_targets))
    ]
    candidates = rotate_targets + general
    logger.info(
        "%d slot(s) | %d rotation target(s) + %d general candidates | Cash: $%.2f",
        slots, len(rotate_targets), len(general), avail_cash,
    )

    filled = 0
    for c in candidates:
        if filled >= slots or avail_cash < 10:
            break
        remaining = slots - filled
        # Leave a $2 buffer per slot to absorb price drift between quote and fill
        allocate  = min(MAX_PER_POS, avail_cash // remaining) - 2
        if allocate <= 0:
            continue
        # Floor shares (never round up) to ensure cost stays at or below allocate
        shares    = math.floor(allocate / c["price"] * 10000) / 10000
        if shares < 0.0001:
            continue
        total = round(shares * c["price"], 2)
        logger.info(
            "[BUY] BUY %s %.4f sh @ $%.2f = $%.2f | score %d | upside %.0f%%",
            c["ticker"], shares, c["price"], total, c["score"], c["upside"],
        )
        try:
            _place_order(session, base_url, game_id, c["ticker"], "buy", shares)
            logger.info("OK %s placed!", c["ticker"])
            avail_cash -= total
            filled += 1
            db.log_trade(
                _run_id, c["ticker"], "buy", shares,
                c["price"], total, c["score"], "scored buy", "placed",
            )
        except Exception as e:
            err = str(e)
            logger.error("✗ BUY %s failed: %s", c["ticker"], err)
            db.log_trade(
                _run_id, c["ticker"], "buy", shares,
                c["price"], total, c["score"], "scored buy", "failed", err,
            )
            # Permanently block tickers rejected for wrong exchange
            if "not in the allowed markets" in err or "not in allowed markets" in err:
                db.blocklist_add(c["ticker"], f"Exchange not allowed: {err[err.find('(')+1:err.find(')')] if '(' in err else 'unknown'}")
                logger.warning("BLOCKED %s permanently — not on an allowed exchange", c["ticker"])

    # -- 6. Final snapshot ------------------------------------------------------
    logger.info("Refreshing portfolio…")
    try:
        upd      = _get(session, base_url, f"/api/games/{game_id}/portfolio")
        cash2    = _cash(upd)
        pos2     = _positions(upd)
        invested2 = sum(_pval(p) for p in pos2)
        pnl2     = (cash2 + invested2) - BUDGET
        db.save_portfolio_snapshot(_run_id, cash2, invested2, pnl2, pos2)
        logger.info(
            "FINAL -> Cash: $%.2f | Invested: $%.2f | Total: $%.2f | P&L: %+.2f",
            cash2, invested2, cash2 + invested2, pnl2,
        )
    except Exception as e:
        logger.error("Could not refresh portfolio: %s", e)

    logger.info("[OK] Done — %d buy(s), %d sell(s). run_id=%s", filled, len(sell_list), _run_id)
    logger.info("=" * 60)


# -- Scheduler -----------------------------------------------------------------

def run_scheduler() -> None:
    times = db.schedule_list()
    if not times:
        print("No scheduled times set. Add one with:")
        print("  python trading_agent.py schedule add HH:MM")
        return

    print(f"Scheduler active. Configured times (local): {', '.join(times)}")
    print("Press Ctrl-C to stop.\n")

    _running = True
    def _stop(sig, frame):
        nonlocal _running
        logger.info("Shutdown signal — stopping scheduler.")
        _running = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    def _local_now() -> datetime:
        tz_name = db.config_get("timezone") or "America/New_York"
        try:
            return datetime.now(tz=ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, KeyError):
            return datetime.now(tz=ZoneInfo("UTC"))

    fired_today: set[str] = set()
    last_date = _local_now().date()

    while _running:
        now = _local_now()
        # Reset fired set at midnight
        if now.date() != last_date:
            fired_today.clear()
            last_date = now.date()

        current_hm = now.strftime("%H:%M")
        times = db.schedule_list()  # re-read so live config changes take effect

        if current_hm in times and current_hm not in fired_today:
            fired_today.add(current_hm)
            logger.info("[ALARM] Scheduled trigger at %s", current_hm)
            try:
                run_agent()
            except Exception as e:
                logger.exception("Agent run failed: %s", e)

        _time.sleep(30)  # check every 30 s — more than precise enough for HH:MM

    logger.info("Scheduler stopped.")


# -- CLI ------------------------------------------------------------------------

def cmd_config_show(_args: argparse.Namespace) -> None:
    cfg = db.config_get_all()
    print("\n  Current configuration")
    print("  " + "-" * 40)
    for k, v in cfg.items():
        print(f"  {k:<20} {v}")
    times = db.schedule_list()
    print(f"  {'schedule_times':<20} {', '.join(times) or '(none)'}")
    print()


def cmd_config_set(args: argparse.Namespace) -> None:
    key, value = args.key, args.value
    allowed = {"base_url", "game_id", "username"}
    if key not in allowed:
        print(f"Unknown config key '{key}'. Allowed: {', '.join(sorted(allowed))}")
        print("To set the API key use: python trading_agent.py config set-api-key")
        sys.exit(1)
    db.config_set(key, value)
    print(f"OK {key} = {value}")


def cmd_set_api_key(_args: argparse.Namespace) -> None:
    print("Enter API key (input hidden):")
    key = getpass.getpass(prompt="  API key: ")
    if not key.strip():
        print("Aborted — empty input.")
        sys.exit(1)
    db.config_set("api_key", key.strip())
    print("OK API key saved (encrypted).")


def cmd_schedule_add(args: argparse.Namespace) -> None:
    t = args.time.strip()
    try:
        dtime.fromisoformat(t + ":00")  # validates HH:MM
    except ValueError:
        print(f"Invalid time '{t}'. Use HH:MM in 24-hour format, e.g. 09:35")
        sys.exit(1)
    db.schedule_add(t)
    print(f"OK Scheduled run added: {t}")
    print(f"  All times: {', '.join(db.schedule_list())}")


def cmd_schedule_remove(args: argparse.Namespace) -> None:
    db.schedule_remove(args.time.strip())
    remaining = db.schedule_list()
    print(f"OK Removed. Remaining: {', '.join(remaining) or '(none)'}")


def cmd_schedule_list(_args: argparse.Namespace) -> None:
    times = db.schedule_list()
    if times:
        print("Scheduled run times (local, HH:MM):")
        for t in times:
            print(f"  {t}")
    else:
        print("No scheduled times configured.")


def cmd_logs(args: argparse.Namespace) -> None:
    rows = db.get_run_logs(tail=args.tail, level=args.level)
    if not rows:
        print("No logs found.")
        return
    for r in rows:
        print(f"{r['logged_at']}  [{r['level']:<7}] [{r['run_id']}] {r['message']}")


def cmd_history(args: argparse.Namespace) -> None:
    trades = db.get_trade_history(limit=args.limit)
    if not trades:
        print("No trade history.")
        return
    fmt = "{:<20}  {:<6}  {:<5}  {:>8}  {:>9}  {:>9}  {:>5}  {:<10}  {}"
    print(fmt.format("Date/Time", "Symbol", "Side", "Shares", "Price", "Total", "Score", "Status", "Reason"))
    print("-" * 110)
    for t in trades:
        print(fmt.format(
            t["executed_at"][:19], t["symbol"], t["side"],
            f"{t['shares']:.4f}", f"${t['price']:.2f}" if t["price"] else "-",
            f"${t['total']:.2f}" if t["total"] else "-",
            str(t["score"] or "-"), t["status"], (t["reason"] or "")[:40],
        ))


def cmd_portfolio(_args: argparse.Namespace) -> None:
    snap = db.get_latest_snapshot()
    if not snap:
        print("No portfolio snapshot in database. Run the agent first.")
        return
    print(f"\n  Portfolio snapshot — {snap['captured_at']}")
    print(f"  Cash:        ${snap['cash']:.2f}")
    print(f"  Invested:    ${snap['invested']:.2f}")
    print(f"  Total:       ${snap['total_value']:.2f}")
    print(f"  P&L:         ${snap['pnl']:+.2f}")
    print(f"  Positions:   {len(snap['positions'])}")
    if snap["positions"]:
        print()
        print(f"  {'Symbol':<8} {'Shares':>8} {'Curr Price':>11} {'Value':>10} {'P&L%':>7}")
        print("  " + "-" * 48)
        for p in snap["positions"]:
            sym   = _sym(p)
            qty   = float(p.get("quantity") or p.get("shares") or 0)
            cost  = float(p.get("cost_basis") or p.get("avg_price") or 0)
            curr  = float(p.get("current_price") or p.get("price") or cost)
            val   = _pval(p)
            pct   = (curr - cost) / cost * 100 if cost else 0
            print(f"  {sym:<8} {qty:>8.4f} {curr:>11.2f} {val:>10.2f} {pct:>+6.1f}%")
    print()


def cmd_update(args: argparse.Namespace) -> None:
    import updater
    try:
        if args.check:
            status = updater.check_for_update()
            if status["up_to_date"]:
                print(f"Up to date (branch {status['branch']} @ {status['current_short']}).")
            else:
                print(f"Update available: {status['current_short']} -> {status['remote_short']} (branch {status['branch']})")
                print(f"{len(status['changelog'])} new commit(s):")
                for line in status["changelog"]:
                    print(f"  {line}")
            return

        for item in updater.apply_update():
            if isinstance(item, tuple) and item[0] == "done":
                _, success, msg = item
                print(msg)
                if success and "Updated to" in msg:
                    print("Restart the service to apply the update:")
                    print("  sudo systemctl restart trading-agent-web")
            else:
                print(item)
    except updater.UpdateError as e:
        print(f"Update failed: {e}")
        sys.exit(1)


# -- Entry point ----------------------------------------------------------------

def main() -> None:
    db.init_db()

    parser = argparse.ArgumentParser(
        prog="trading_agent.py",
        description="Headless Trading Agent — all data stored in SQLite",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # config
    cfg_p = sub.add_parser("config", help="View or change configuration")
    cfg_sub = cfg_p.add_subparsers(dest="config_cmd", required=True)

    cfg_sub.add_parser("show", help="Print current configuration")

    cfg_set = cfg_sub.add_parser("set", help="Set a config value (base_url, game_id, username)")
    cfg_set.add_argument("key",   help="Config key")
    cfg_set.add_argument("value", help="New value")

    cfg_sub.add_parser("set-api-key", help="Set API key (prompted, hidden input)")

    # schedule
    sch_p = sub.add_parser("schedule", help="Manage and run the scheduler")
    sch_sub = sch_p.add_subparsers(dest="sch_cmd", required=True)

    sch_add = sch_sub.add_parser("add", help="Add a run time (HH:MM)")
    sch_add.add_argument("time", help="HH:MM in 24-hour local time")

    sch_rm = sch_sub.add_parser("remove", help="Remove a run time (HH:MM)")
    sch_rm.add_argument("time", help="HH:MM to remove")

    sch_sub.add_parser("list", help="List configured run times")
    sch_sub.add_parser("run",  help="Start the scheduler daemon")

    # run
    sub.add_parser("run", help="Run the agent once immediately")

    # logs
    log_p = sub.add_parser("logs", help="View recent log entries from the database")
    log_p.add_argument("--tail",  type=int, default=100, help="Number of lines (default 100)")
    log_p.add_argument("--level", default=None, help="Filter by level (INFO, WARNING, ERROR)")

    # history
    hist_p = sub.add_parser("history", help="View trade history from the database")
    hist_p.add_argument("--limit", type=int, default=50, help="Number of trades (default 50)")

    # portfolio
    sub.add_parser("portfolio", help="Show latest portfolio snapshot from the database")

    # update
    upd_p = sub.add_parser("update", help="Check for or apply updates via git")
    upd_p.add_argument("--check", action="store_true", help="Only check for updates, don't apply")

    args = parser.parse_args()

    if args.command == "config":
        if   args.config_cmd == "show":        cmd_config_show(args)
        elif args.config_cmd == "set":         cmd_config_set(args)
        elif args.config_cmd == "set-api-key": cmd_set_api_key(args)

    elif args.command == "schedule":
        if   args.sch_cmd == "add":    cmd_schedule_add(args)
        elif args.sch_cmd == "remove": cmd_schedule_remove(args)
        elif args.sch_cmd == "list":   cmd_schedule_list(args)
        elif args.sch_cmd == "run":    run_scheduler()

    elif args.command == "run":
        run_agent()

    elif args.command == "logs":
        cmd_logs(args)

    elif args.command == "history":
        cmd_history(args)

    elif args.command == "portfolio":
        cmd_portfolio(args)

    elif args.command == "update":
        cmd_update(args)


if __name__ == "__main__":
    main()
