#!/bin/bash
# =============================================================================
#  Stock Trading Agent — Installer
# =============================================================================
#  Usage:
#    sudo bash install.sh               Interactive (recommended)
#    sudo bash install.sh --port 8080   Set port without prompting
#    sudo bash install.sh --help        Show this help
# =============================================================================

set -e

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}  [*]${NC} $*"; }
ok()      { echo -e "${GREEN}  [+]${NC} $*"; }
warn()    { echo -e "${YELLOW}  [!]${NC} $*"; }
die()     { echo -e "${RED}  [X]${NC} $*"; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}=== $* ===${NC}"; }

# ── Help ──────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo ""
  echo -e "${BOLD}Stock Trading Agent — Installer${NC}"
  echo ""
  echo "  Usage:"
  echo "    sudo bash install.sh               Interactive (recommended)"
  echo "    sudo bash install.sh --port 8080   Non-interactive port selection"
  echo ""
  exit 0
fi

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run this with sudo:  sudo bash install.sh"

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SVC_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
SERVICE="trading-agent-web"
PYTHON="python3"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║       Stock Trading Agent Installer      ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Installing to : $INSTALL_DIR"
echo "  Service user  : $SVC_USER"
echo ""

# ── Re-install guard ──────────────────────────────────────────────────────────
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  warn "Service '$SERVICE' is already running."
  read -rp "  Stop it and re-install? [y/N]: " ANS
  [[ "${ANS,,}" == "y" ]] || { echo "  Aborted."; exit 0; }
  systemctl stop "$SERVICE"
fi

# ── Port ──────────────────────────────────────────────────────────────────────
header "Web UI Port"

PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port|-p)  PORT="$2"; shift 2 ;;
    --port=*)   PORT="${1#*=}"; shift ;;
    --help|-h)  shift ;;
    *)          die "Unknown argument: $1" ;;
  esac
done

if [[ -z "$PORT" ]]; then
  echo "  Common ports: 5000 (default), 8080, 8000"
  while true; do
    read -rp "  Port [5000]: " PORT
    PORT="${PORT:-5000}"
    [[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) && break
    warn "'$PORT' is not valid — enter a number 1-65535"
  done
fi
ok "Port: $PORT"

# ── Step 1: System packages via apt ───────────────────────────────────────────
# Using apt instead of pip avoids venv/PEP-668/network-via-sudo issues entirely.
# All three packages are in the standard Raspberry Pi OS / Debian repo.
header "Step 1/5 — System Packages"

apt-get update -qq

PACKAGES=(
  python3               # runtime
  python3-requests      # HTTP client
  python3-flask         # web framework
  python3-cryptography  # API key encryption
  tzdata                # IANA timezone database (needed by zoneinfo)
)

info "Installing: ${PACKAGES[*]}"
DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}"
ok "All packages installed"

# ── Step 2: Verify Python imports ─────────────────────────────────────────────
header "Step 2/5 — Verify Imports"

$PYTHON - <<'PYCHECK'
import sys
missing = []
for mod in ("requests", "flask", "cryptography"):
    try:
        __import__(mod)
    except ImportError:
        missing.append(mod)
if missing:
    print(f"MISSING: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
print("  requests, flask, cryptography — all OK")
PYCHECK
ok "Python imports verified"

# ── Step 3: Initialise database & encryption key ──────────────────────────────
header "Step 3/5 — Database Setup"

cd "$INSTALL_DIR"
if [[ -f trading_agent.db ]]; then
  ok "Existing database kept intact"
else
  info "Creating database..."
  $PYTHON trading_agent.py config show > /dev/null 2>&1 || true
  ok "Database created"
fi

if [[ -f .keyfile ]]; then
  ok "Encryption keyfile already exists"
else
  info "Generating encryption keyfile..."
  $PYTHON trading_agent.py config show > /dev/null 2>&1 || true
  [[ -f .keyfile ]] && { chmod 600 .keyfile; ok "Keyfile created (.keyfile)"; } \
                    || warn "Keyfile not created — check Python is working"
fi

# Fix ownership so the service user can read/write everything
chown -R "$SVC_USER":"$SVC_USER" "$INSTALL_DIR"
ok "Ownership set to $SVC_USER"

# ── Step 4: Configuration prompts (first install only) ────────────────────────
if ! $PYTHON -c "
import sys, os
sys.path.insert(0,'$INSTALL_DIR')
import db; db.init_db()
sys.exit(0 if db.config_get('api_key') else 1)
" 2>/dev/null; then

  header "Step 4/5 — Agent Configuration"
  echo "  Press Enter to accept the default shown in brackets."
  echo ""

  read -rp "  Game ID   [1]: " G; G="${G:-1}"
  $PYTHON trading_agent.py config set game_id "$G"
  ok "Game ID: $G"

  read -rp "  Username  [optional]: " U
  [[ -n "$U" ]] && { $PYTHON trading_agent.py config set username "$U"; ok "Username: $U"; }

  read -rp "  Base URL  [https://stocks.namoh.net]: " B
  B="${B:-https://stocks.namoh.net}"
  $PYTHON trading_agent.py config set base_url "$B"
  ok "Base URL: $B"

  echo ""
  echo "  Enter your API key (input is hidden and stored encrypted):"
  read -rsp "  API Key: " APIKEY; echo ""
  if [[ -n "$APIKEY" ]]; then
    echo "$APIKEY" | $PYTHON - <<'PYKEY'
import sys, os
sys.path.insert(0, os.getcwd())
import db; db.init_db()
db.config_set("api_key", sys.stdin.read().strip())
PYKEY
    ok "API key saved (encrypted)"
  else
    warn "No API key entered — add it later via the web UI Config page"
  fi

  echo ""
  header "Step 4/5 — Schedule Setup"
  echo "  Add daily run times in HH:MM (24-hour local time)."
  echo "  Suggestions: 09:35  13:00  15:45"
  echo "  Press Enter with no input when done."
  echo ""
  while true; do
    read -rp "  Add time (or Enter to skip): " T
    [[ -z "$T" ]] && break
    if [[ "$T" =~ ^[0-2][0-9]:[0-5][0-9]$ ]]; then
      $PYTHON trading_agent.py schedule add "$T" > /dev/null
      ok "Scheduled: $T"
    else
      warn "Use HH:MM format, e.g. 09:35"
    fi
  done

else
  header "Step 4/5 — Configuration"
  ok "Existing configuration kept — skipping setup prompts"
  ok "Edit settings anytime via the web UI Config page"
fi

# ── Step 5: Systemd service ───────────────────────────────────────────────────
header "Step 5/5 — System Service"

cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Stock Trading Agent Web UI
After=network.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON} ${INSTALL_DIR}/web.py --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" --quiet
systemctl start  "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  ok "Service is running"
else
  warn "Service did not start. Run this to see why:"
  echo "       sudo journalctl -u $SERVICE -n 30 --no-pager"
  exit 1
fi

# ── Firewall ──────────────────────────────────────────────────────────────────
if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "^Status: active"; then
  if ufw status 2>/dev/null | grep -q "^${PORT}"; then
    ok "ufw: port $PORT already allowed"
  else
    ufw allow "${PORT}/tcp" > /dev/null
    ok "ufw: allowed port $PORT"
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║         Installation Complete!           ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Open in your browser:"
echo -e "    ${CYAN}http://$(hostname).local:${PORT}${NC}"
[[ -n "$LOCAL_IP" ]] && echo -e "    ${CYAN}http://${LOCAL_IP}:${PORT}${NC}"
echo ""
echo "  Manage the service:"
echo "    sudo systemctl status  $SERVICE"
echo "    sudo systemctl restart $SERVICE"
echo "    sudo journalctl -u $SERVICE -f"
echo ""
