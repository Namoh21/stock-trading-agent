#!/usr/bin/env python3
"""
web.py — Flask web interface for the Trading Agent.

Run:
  python web.py                        # default port 5000
  python web.py --port 8080            # custom port
  python web.py --host 0.0.0.0         # expose on LAN (Pi default)

Access from any device on the same network:
  http://raspberrypi.local:5000
"""

import argparse
import json
import os
import threading
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, Response
)

import db
import trading_agent as agent
import updater

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "tA-w3b-s3cr3t-ch4ng3-m3"

# ── Timezone helpers ───────────────────────────────────────────────────────────

def get_tz() -> ZoneInfo:
    """Return the configured timezone, falling back to UTC on bad config."""
    tz_name = db.config_get("timezone") or "America/New_York"
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")

def now_local() -> datetime:
    return datetime.now(tz=get_tz())

@app.template_filter("localtime")
def localtime_filter(utc_str: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Convert a UTC ISO string from the DB into the configured local timezone."""
    if not utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_str).replace(tzinfo=timezone.utc)
        return dt.astimezone(get_tz()).strftime(fmt)
    except (ValueError, TypeError):
        return utc_str[:19]

@app.context_processor
def inject_tz():
    """Make tz_name available in every template."""
    return {"tz_name": db.config_get("timezone") or "America/New_York"}

db.init_db()

# ── Agent state (in-memory, single-process) ────────────────────────────────────
_lock         = threading.Lock()
_agent_state  = {
    "running":       False,
    "run_id":        None,
    "started_at":    None,
    "last_finished": None,
}

# ── Background scheduler ────────────────────────────────────────────────────────
import time as _time
import signal as _signal

_scheduler_thread = None
_scheduler_stop   = threading.Event()


def _scheduler_loop() -> None:
    fired_today: set[str] = set()
    last_date = now_local().date()
    while not _scheduler_stop.is_set():
        now = now_local()
        if now.date() != last_date:
            fired_today.clear()
            last_date = now.date()
        current_hm = now.strftime("%H:%M")
        times = db.schedule_list()
        if current_hm in times and current_hm not in fired_today:
            fired_today.add(current_hm)
            _run_agent_background(triggered_by="scheduler")
        _scheduler_stop.wait(30)


def _run_agent_background(triggered_by: str = "manual") -> str | None:
    """Start the agent in a background thread. Returns run_id or None if busy."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        if _agent_state["running"]:
            _lock.release()
            return None
        _agent_state["running"]    = True
        _agent_state["started_at"] = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")

        def _run():
            try:
                agent.run_agent()
            except Exception as e:
                logging.getLogger("agent").error("Agent error: %s", e)
            finally:
                _agent_state["running"]       = False
                _agent_state["last_finished"] = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
                _lock.release()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # run_id is set inside run_agent() — grab it after a brief yield
        _time.sleep(0.1)
        _agent_state["run_id"] = agent._run_id
        return agent._run_id
    except Exception:
        _agent_state["running"] = False
        _lock.release()
        return None


# ── Routes — Dashboard ─────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    snap   = db.get_latest_snapshot()
    recent = db.get_run_logs(tail=15)
    times  = db.schedule_list()
    cfg    = db.config_get_all()
    top_scores = db.get_latest_scores()
    return render_template(
        "dashboard.html",
        snap=snap,
        recent=recent,
        times=times,
        cfg=cfg,
        state=_agent_state,
        top_scores=top_scores,
    )


@app.route("/agent/run", methods=["POST"])
def trigger_run():
    run_id = _run_agent_background(triggered_by="web")
    if run_id is None:
        flash("Agent is already running.", "warning")
    else:
        flash(f"Agent started (run_id: {run_id})", "success")
    return redirect(url_for("dashboard"))


# ── Routes — Config ────────────────────────────────────────────────────────────

@app.route("/config")
def config_page():
    cfg = db.config_get_all()
    return render_template("config.html", cfg=cfg)


@app.route("/config/save", methods=["POST"])
def config_save():
    for key in ("base_url", "game_id", "username"):
        val = request.form.get(key, "").strip()
        if val:
            db.config_set(key, val)
    # Checkbox — present means "1", absent means "0"
    db.config_set("trade_metals", "1" if request.form.get("trade_metals") else "0")
    tz = request.form.get("timezone", "").strip()
    if tz:
        try:
            ZoneInfo(tz)  # validate before saving
            db.config_set("timezone", tz)
        except (ZoneInfoNotFoundError, KeyError):
            flash(f"Unknown timezone '{tz}' — not saved. Use an IANA name like America/New_York.", "warning")
            return redirect(url_for("config_page"))
    flash("Configuration saved.", "success")
    return redirect(url_for("config_page"))


@app.route("/config/set-api-key", methods=["POST"])
def config_set_api_key():
    key = request.form.get("api_key", "").strip()
    if not key:
        flash("API key cannot be empty.", "danger")
        return redirect(url_for("config_page"))
    db.config_set("api_key", key)
    flash("API key saved (encrypted).", "success")
    return redirect(url_for("config_page"))


# ── Routes — Schedule ──────────────────────────────────────────────────────────

@app.route("/schedule")
def schedule_page():
    times = db.schedule_list()
    return render_template("schedule.html", times=times)


@app.route("/schedule/add", methods=["POST"])
def schedule_add():
    t = request.form.get("run_time", "").strip()
    if not t or len(t) != 5 or t[2] != ":":
        flash("Invalid time — use HH:MM format (e.g. 09:35)", "danger")
        return redirect(url_for("schedule_page"))
    db.schedule_add(t)
    flash(f"Scheduled time {t} added.", "success")
    return redirect(url_for("schedule_page"))


@app.route("/schedule/remove", methods=["POST"])
def schedule_remove():
    t = request.form.get("run_time", "").strip()
    db.schedule_remove(t)
    flash(f"Scheduled time {t} removed.", "success")
    return redirect(url_for("schedule_page"))


# ── Routes — Logs ──────────────────────────────────────────────────────────────

@app.route("/logs")
def logs_page():
    level  = request.args.get("level", "")
    tail   = int(request.args.get("tail", 200))
    run_id = request.args.get("run_id", "")
    rows   = db.get_run_logs(tail=tail, level=level or None)
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    return render_template("logs.html", rows=rows, level=level, tail=tail, run_id=run_id)


# ── Routes — History ───────────────────────────────────────────────────────────

@app.route("/history")
def history_page():
    limit  = int(request.args.get("limit", 100))
    trades = db.get_trade_history(limit=limit)
    return render_template("history.html", trades=trades, limit=limit)


# ── Routes — Portfolio ─────────────────────────────────────────────────────────

@app.route("/portfolio")
def portfolio_page():
    snap = db.get_latest_snapshot()
    return render_template("portfolio.html", snap=snap)


# ── Routes — Update ────────────────────────────────────────────────────────────

@app.route("/update")
def update_page():
    is_repo = updater.is_git_repo()
    branch = current_short = None
    if is_repo:
        try:
            branch        = updater._git("rev-parse", "--abbrev-ref", "HEAD")
            current_short = updater._git("rev-parse", "--short", "HEAD")
        except updater.UpdateError:
            pass
    return render_template("update.html", is_repo=is_repo, branch=branch, current_short=current_short)


@app.route("/update/check")
def update_check():
    try:
        return jsonify(updater.check_for_update())
    except updater.UpdateError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/update/apply")
def update_apply():
    def stream():
        success = False
        for item in updater.apply_update():
            if isinstance(item, tuple) and item[0] == "done":
                _, success, msg = item
                yield f"event: done\ndata: {json.dumps({'success': success, 'msg': msg})}\n\n"
            else:
                yield f"event: log\ndata: {json.dumps({'msg': item})}\n\n"
        if success:
            # Exit non-zero so systemd (Restart=on-failure) brings the
            # service back up running the freshly-pulled code.
            def _restart():
                _time.sleep(0.5)
                os._exit(1)
            threading.Thread(target=_restart, daemon=True).start()

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })


# ── API — JSON endpoints (used by JS polling) ──────────────────────────────────

@app.route("/blocklist")
def blocklist_page():
    return render_template("blocklist.html", entries=db.blocklist_all())

@app.route("/blocklist/add", methods=["POST"])
def blocklist_add():
    symbol = request.form.get("symbol", "").strip().upper()
    reason = request.form.get("reason", "").strip() or "Manually blocked"
    if not symbol:
        flash("Symbol is required.", "danger")
        return redirect(url_for("blocklist_page"))
    db.blocklist_add(symbol, reason)
    flash(f"{symbol} added to blocklist.", "success")
    return redirect(url_for("blocklist_page"))

@app.route("/blocklist/remove", methods=["POST"])
def blocklist_remove():
    symbol = request.form.get("symbol", "").strip().upper()
    db.blocklist_remove(symbol)
    flash(f"{symbol} removed from blocklist.", "success")
    return redirect(url_for("blocklist_page"))


@app.route("/api/status")
def api_status():
    snap = db.get_latest_snapshot()
    return jsonify({
        "agent":     _agent_state,
        "portfolio": {
            "cash":        snap["cash"]        if snap else None,
            "invested":    snap["invested"]    if snap else None,
            "total_value": snap["total_value"] if snap else None,
            "pnl":         snap["pnl"]         if snap else None,
            "positions":   len(snap["positions"]) if snap else 0,
            "captured_at": snap["captured_at"] if snap else None,
        },
        "schedule": db.schedule_list(),
    })


@app.route("/api/logs/recent")
def api_logs_recent():
    run_id = request.args.get("run_id", "")
    tail   = int(request.args.get("tail", 50))
    rows   = db.get_run_logs(tail=tail)
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    return jsonify(rows)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trading Agent Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default 5000)")
    parser.add_argument("--no-scheduler", action="store_true", help="Disable built-in scheduler")
    args = parser.parse_args()

    if not args.no_scheduler:
        t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
        t.start()
        print(f"[scheduler] Running in background. Times: {db.schedule_list() or '(none set)'}")

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # suppress dev-server warning

    print(f"[web] Starting on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
