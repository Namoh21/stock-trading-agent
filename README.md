# Stock Trading Agent

A headless stock trading agent designed to run on a **Raspberry Pi** (or any Linux machine). It scores stocks using momentum and technical signals, automatically buys and sells based on configurable rules, and exposes a clean **web dashboard** accessible from any device on your network.

All configuration, trade history, logs, and portfolio snapshots are stored in a local **SQLite database**. The API key is stored **encrypted** — nothing sensitive is ever written to a plaintext file.

---

## Features

- **Web UI** — dashboard, portfolio, trade history, live logs, schedule manager, and config — all in a browser
- **Automated scheduling** — set run times (e.g. `09:35`, `13:00`) and the agent fires automatically
- **Smart scoring** — ranks stocks by day momentum, 52-week position, and volume; rotates into better opportunities
- **Encrypted secrets** — API key stored with Fernet symmetric encryption; never in plaintext
- **Full audit trail** — every buy, sell, log line, and portfolio snapshot saved to SQLite and queryable via the UI or CLI
- **CLI** — full command-line interface for headless / SSH use

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.10 or newer |
| pip | any recent version |
| OS | Raspberry Pi OS, Ubuntu, Debian, macOS, Windows |

Python packages (installed automatically):
- `requests` — HTTP calls to the trading API
- `cryptography` — Fernet encryption for the API key
- `flask` — web interface

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/Namoh21/stock-trading-agent.git
cd stock-trading-agent
```

### 2. Run the installer

```bash
sudo bash install.sh
```

The installer will:
- Ask for a web UI port (default `5000`)
- Install Python dependencies
- Walk you through entering your API key and game settings
- Create and start a `systemd` service so the agent survives reboots

> **Non-interactive install** (e.g. for scripting):
> ```bash
> sudo bash install.sh --port 8080
> ```

### 3. Open the web UI

After install, open a browser on any device on the same network:

```
http://raspberrypi.local:5000
```

Or use the IP address printed at the end of the installer.

---

## First-Time Setup (Web UI)

1. Go to **Config** in the sidebar
2. Enter your **API Key** — it is encrypted before being saved
3. Set your **Game ID**, **Username**, and **Base URL**
4. Go to **Schedule** and add at least one run time (e.g. `09:35`)
5. Return to the **Dashboard** and click **Run Agent Now** to test

---

## Web Interface

| Page | What it shows |
|---|---|
| **Dashboard** | Portfolio value cards, current holdings, recent log tail, Run button |
| **Portfolio** | Full position breakdown with avg cost, current price, gain/loss per holding |
| **Trade History** | Every buy and sell with score, price, total, and status |
| **Logs** | Filterable log viewer (by level and run ID); auto-scrolls during live runs |
| **Schedule** | Add/remove daily run times; suggested market-hour presets |
| **Config** | Game ID, URL, username, and encrypted API key management |

The dashboard **auto-refreshes** portfolio cards and agent status every 8 seconds. When the agent is running, the log panel refreshes every 3 seconds.

---

## Command-Line Interface

The agent also has a full CLI for SSH / headless use:

```bash
# Configuration
python3 trading_agent.py config show
python3 trading_agent.py config set game_id 2
python3 trading_agent.py config set base_url https://stocks.namoh.net
python3 trading_agent.py config set username brian
python3 trading_agent.py config set-api-key        # hidden prompt

# Schedule management
python3 trading_agent.py schedule add 09:35
python3 trading_agent.py schedule add 13:00
python3 trading_agent.py schedule list
python3 trading_agent.py schedule remove 13:00
python3 trading_agent.py schedule run              # start scheduler daemon

# Run the agent
python3 trading_agent.py run                       # run once immediately

# View history
python3 trading_agent.py logs --tail 100 --level ERROR
python3 trading_agent.py history --limit 50
python3 trading_agent.py portfolio
```

---

## Running the Web Server

```bash
# Default (port 5000, all interfaces)
python3 web.py

# Custom port
python3 web.py --port 8080

# Localhost only (no LAN access)
python3 web.py --host 127.0.0.1 --port 5000

# Disable the built-in scheduler (if running CLI scheduler separately)
python3 web.py --no-scheduler
```

---

## File Structure

```
stock-trading-agent/
├── trading_agent.py     # Core agent logic + CLI
├── web.py               # Flask web interface + built-in scheduler
├── db.py                # SQLite database layer + Fernet encryption
├── templates/           # Jinja2 HTML templates (dark theme)
│   ├── base.html
│   ├── dashboard.html
│   ├── portfolio.html
│   ├── history.html
│   ├── logs.html
│   ├── schedule.html
│   └── config.html
├── requirements.txt
├── install.sh
└── .gitignore           # Excludes trading_agent.db, .keyfile, logs/
```

### Files that are never committed to git

| File | Contains |
|---|---|
| `trading_agent.db` | All data — portfolio, trades, logs, config |
| `.keyfile` | Fernet encryption key — **back this up** |
| `logs/` | Any legacy log files |

> **Important:** If you lose `.keyfile`, the stored API key cannot be decrypted. Back it up to a safe location.

---

## Strategy Overview

Each agent run follows these steps:

1. **Fetch portfolio** — current cash, positions, and P&L
2. **Discover tickers** — from the API's stock list, or a built-in fallback list
3. **Score every stock** — composite score (0–100) based on:
   - Day % change (momentum)
   - Position within 52-week range
   - Volume vs average volume
4. **Sell analysis** — sells a position if:
   - Score drops below 40 ("weak hold")
   - Gain exceeds +30% with a score below 75 (take profits)
   - Loss exceeds −15% (cut loss)
   - A significantly better-ranked stock is available (rotation)
5. **Buy analysis** — fills open slots with highest-ranked stocks above the minimum score threshold
6. **Save snapshot** — portfolio state saved to DB before and after

### Key constants (in `trading_agent.py`)

| Constant | Default | Description |
|---|---|---|
| `BUDGET` | `10,000` | Starting cash for P&L calculation |
| `MAX_POSITIONS` | `8` | Maximum simultaneous positions |
| `MAX_PER_POS` | `2,000` | Max dollars allocated per position |
| `MIN_SCORE` | `60` | Minimum score to buy |
| `ROTATION_THRESHOLD` | `20` | Score gap needed to rotate out of a losing position |
| `ROTATION_GAP_PROFITABLE` | `30` | Score gap needed to rotate out of a profitable position |

---

## Systemd Service

The installer creates a service called `trading-agent-web` that:
- Starts automatically on boot
- Restarts on failure (15 s delay)
- Runs the web UI **and** the scheduler in one process

```bash
# Common service commands
sudo systemctl status trading-agent-web
sudo systemctl restart trading-agent-web
sudo systemctl stop trading-agent-web
sudo journalctl -u trading-agent-web -f     # live service logs
```

---

## Troubleshooting

**Web UI not loading**
- Check the service is running: `sudo systemctl status trading-agent-web`
- Check the port isn't blocked by a firewall: `sudo ufw allow 5000/tcp`
- Try accessing by IP instead of hostname: `http://192.168.x.x:5000`

**"API key not set" error**
- Go to **Config** in the web UI and enter your API key, or run:
  `python3 trading_agent.py config set-api-key`

**Agent runs but places no orders**
- Check the **Logs** page for `BUY`/`SELL` lines and error messages
- Verify your Game ID matches your account at the trading platform

**Lost the `.keyfile`**
- The API key stored in the database cannot be recovered without it
- Delete `trading_agent.db` and re-run setup: all config and history will be reset
- Re-enter your API key via the Config page

**Re-running the installer**
- Safe to run again — it will prompt for a new port and overwrite the systemd unit
- Existing database and `.keyfile` are untouched

---

## License

MIT
