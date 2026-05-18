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
RED='\033[0;31m';  GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m';  BOLD='\033[1m';  NC='\033[0m'

info()    { echo -e "${CYAN}  →${NC}  $*"; }
success() { echo -e "${GREEN}  ✔${NC}  $*"; }
warn()    { echo -e "${YELLOW}  ⚠${NC}  $*"; }
error()   { echo -e "${RED}  ✘${NC}  $*"; }
header()  { echo -e "\n${BOLD}${BLUE}══  $*  ${NC}"; }
divider() { echo -e "${BLUE}──────────────────────────────────────────────────${NC}"; }

# ── Help ──────────────────────────────────────────────────────────────────────
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
  echo ""
  echo -e "${BOLD}Stock Trading Agent — Installer${NC}"
  echo ""
  echo "  Usage:"
  echo "    sudo bash install.sh               Interactive install (recommended)"
  echo "    sudo bash install.sh --port 8080   Set web UI port non-interactively"
  echo ""
  echo "  Options:"
  echo "    --port, -p <number>   Web UI port (1–65535). Default: 5000"
  echo "    --help, -h            Show this help message"
  echo ""
  exit 0
fi

# ── Must run as root ──────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  error "This script must be run with sudo."
  echo  "  Try: sudo bash install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_USER="${SUDO_USER:-pi}"
SERVICE_NAME="trading-agent-web"
DB_FILE="$SCRIPT_DIR/trading_agent.db"
KEY_FILE="$SCRIPT_DIR/.keyfile"
REINSTALL=false

# ── Banner ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║       Stock Trading Agent Installer      ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Detect re-install ─────────────────────────────────────────────────────────
if systemctl list-units --full -all 2>/dev/null | grep -q "${SERVICE_NAME}.service"; then
  REINSTALL=true
  warn "Existing installation detected."
  echo ""
  echo -e "  ${YELLOW}Re-installing will:${NC}"
  echo "    • Update Python dependencies"
  echo "    • Update the systemd service (new port if changed)"
  echo "    • Leave your database and .keyfile untouched"
  echo ""
  read -rp "  Continue with re-install? [Y/n]: " CONFIRM
  CONFIRM="${CONFIRM:-Y}"
  if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "  Aborted."
    exit 0
  fi
fi

# ── Install system prerequisites ──────────────────────────────────────────────
header "Installing System Prerequisites"

# Detect package manager
if command -v apt-get &>/dev/null; then
  PKG_MANAGER="apt"
elif command -v dnf &>/dev/null; then
  PKG_MANAGER="dnf"
elif command -v pacman &>/dev/null; then
  PKG_MANAGER="pacman"
else
  PKG_MANAGER="unknown"
fi

# ── Network connectivity check ─────────────────────────────────────────────────
header "Checking Network Connectivity"

check_network() {
  # Try HTTPS reachability to PyPI and fallback hosts
  for host in pypi.org 8.8.8.8 1.1.1.1; do
    if curl -sf --max-time 5 --connect-timeout 5 "https://$host" &>/dev/null \
    || ping -c 1 -W 3 "$host" &>/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

if check_network; then
  success "Internet connection OK"
else
  error "No internet connection detected."
  echo ""
  echo -e "  ${YELLOW}Network status:${NC}"
  echo ""

  # Show IP addresses
  echo -e "  ${CYAN}Network interfaces:${NC}"
  ip -brief addr 2>/dev/null | sed 's/^/    /' || ifconfig 2>/dev/null | grep -E "^[a-z]|inet " | sed 's/^/    /'
  echo ""

  # Show default gateway
  echo -e "  ${CYAN}Default gateway:${NC}"
  ip route show default 2>/dev/null | sed 's/^/    /' || echo "    (none found)"
  echo ""

  echo -e "  ${YELLOW}Troubleshooting steps:${NC}"
  echo ""
  echo "  1. Check your ethernet cable is plugged in, or connect to WiFi:"
  echo "       sudo raspi-config  →  System Options → Wireless LAN"
  echo ""
  echo "  2. Check if you have an IP address:"
  echo "       ip addr show"
  echo ""
  echo "  3. Test your gateway:"
  echo "       ip route show default"
  echo "       ping -c 3 \$(ip route | awk '/default/{print \$3}')"
  echo ""
  echo "  4. Test DNS:"
  echo "       ping -c 3 8.8.8.8    (IP — bypasses DNS)"
  echo "       ping -c 3 pypi.org   (hostname — tests DNS)"
  echo ""
  echo "  5. If using WiFi on a fresh Pi OS install:"
  echo "       sudo nmcli dev wifi connect \"YourSSID\" password \"YourPassword\""
  echo "       # or use the desktop network manager"
  echo ""
  echo "  Once connected, re-run:  sudo bash install.sh"
  echo ""
  exit 1
fi

apt_install() {
  # Install packages only if missing; suppress "already newest version" noise
  local pkgs=()
  for pkg in "$@"; do
    dpkg -s "$pkg" &>/dev/null 2>&1 || pkgs+=("$pkg")
  done
  if [[ ${#pkgs[@]} -gt 0 ]]; then
    info "Installing: ${pkgs[*]}"
    apt-get install -y "${pkgs[@]}" 2>&1 | grep -v "already the newest" || true
  fi
}

if [[ "$PKG_MANAGER" == "apt" ]]; then
  info "Updating package list..."
  apt-get update -qq

  # Core runtime
  apt_install python3 python3-full python3-pip git curl

  # iproute2 provides the `ss` command used for port checking
  apt_install iproute2

  success "System packages ready"

elif [[ "$PKG_MANAGER" == "dnf" ]]; then
  info "Installing prerequisites via dnf..."
  dnf install -y python3 python3-pip git curl iproute 2>&1 | tail -3
  success "System packages ready"

elif [[ "$PKG_MANAGER" == "pacman" ]]; then
  info "Installing prerequisites via pacman..."
  pacman -Sy --noconfirm python python-pip git curl iproute2 2>&1 | tail -3
  success "System packages ready"

else
  warn "Unrecognised package manager — skipping automatic prerequisite install."
  warn "Make sure python3 (3.10+), python3-venv, git, and curl are installed."
fi

# ── Verify Python ──────────────────────────────────────────────────────────────
header "Verifying Python"

PYTHON_BIN=""
for bin in python3 python; do
  if command -v "$bin" &>/dev/null; then
    if "$bin" -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
      PYTHON_BIN="$bin"
      success "Python: $("$bin" --version)"
      break
    else
      warn "Found $("$bin" --version) — need 3.10 or newer."
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  error "Python 3.10+ not found after install attempt. Please install it manually and re-run."
  exit 1
fi

# Verify venv module
if ! "$PYTHON_BIN" -m venv --help &>/dev/null 2>&1; then
  error "python3-venv not available even after installing python3-full."
  echo "  Try: sudo apt install -y python3-full"
  exit 1
fi
success "python3-venv available"

# ── Port selection ─────────────────────────────────────────────────────────────
header "Web UI Port"

PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port|-p) PORT="$2"; shift 2 ;;
    --port=*)  PORT="${1#*=}"; shift ;;
    --help|-h) shift ;;
    *)         error "Unknown argument: $1"; exit 1 ;;
  esac
done

validate_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

is_port_in_use() {
  ss -tlnp 2>/dev/null | grep -q ":$1 " || \
  netstat -tlnp 2>/dev/null | grep -q ":$1 " || false
}

if [[ -n "$PORT" ]]; then
  if ! validate_port "$PORT"; then
    error "Invalid port '$PORT'. Must be a number between 1 and 65535."
    exit 1
  fi
else
  echo -e "  ${CYAN}Which port should the web UI listen on?${NC}"
  echo "  Common choices: 5000 (default), 8080, 8000, 3000"
  echo ""
  while true; do
    read -rp "  Port [default: 5000]: " PORT
    PORT="${PORT:-5000}"
    if ! validate_port "$PORT"; then
      warn "  '$PORT' is not a valid port. Enter a number between 1 and 65535."
      continue
    fi
    if is_port_in_use "$PORT"; then
      warn "Port $PORT appears to be in use already."
      read -rp "  Use it anyway? [y/N]: " FORCE
      [[ "$FORCE" =~ ^[Yy]$ ]] && break
    else
      break
    fi
  done
fi

success "Web UI will be served on port $PORT"

# ── Virtual environment ────────────────────────────────────────────────────────
header "Setting Up Python Environment"

VENV_DIR="$SCRIPT_DIR/.venv"

if [[ -d "$VENV_DIR" ]]; then
  success "Virtual environment already exists — reusing it"
else
  info "Creating virtual environment at $VENV_DIR ..."
  # Create as root; we fix ownership afterward so the service user can write to it
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  success "Virtual environment created"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# ── Install dependencies ──────────────────────────────────────────────────────
header "Installing Python Dependencies"

# Run pip as root so it inherits the full network stack.
# sudo -u strips environment variables (including routing) on some Pi OS builds,
# which causes "Network is unreachable" even when the Pi is online.
info "Installing packages into virtual environment..."
if "$VENV_PIP" install \
    -r "$SCRIPT_DIR/requirements.txt" \
    --timeout 30 \
    --quiet; then
  success "All dependencies installed"
else
  # Retry once with verbose output so the user can see what failed
  warn "First attempt failed — retrying with verbose output..."
  if "$VENV_PIP" install \
      -r "$SCRIPT_DIR/requirements.txt" \
      --timeout 60 \
      --retries 5; then
    success "All dependencies installed (on retry)"
  else
    error "pip install failed. Common causes:"
    echo "    • No internet connection  →  check: ping pypi.org"
    echo "    • Proxy required          →  export https_proxy=http://host:port and re-run"
    echo "    • PyPI temporarily down   →  wait a few minutes and re-run"
    exit 1
  fi
fi

# Fix ownership so the service user can read/write the venv at runtime
chown -R "$PI_USER":"$PI_USER" "$VENV_DIR"

# ── Initialise database ───────────────────────────────────────────────────────
header "Initialising Database"

if [[ -f "$DB_FILE" ]]; then
  success "Existing database found — keeping your data"
else
  info "Creating new database..."
  "$VENV_PYTHON" "$SCRIPT_DIR/trading_agent.py" config show &>/dev/null || true
  success "Database created at $DB_FILE"
fi

if [[ -f "$KEY_FILE" ]]; then
  success "Encryption keyfile found"
else
  info "Generating encryption keyfile (this stores your API key safely)..."
  "$VENV_PYTHON" "$SCRIPT_DIR/trading_agent.py" config show &>/dev/null || true
  if [[ -f "$KEY_FILE" ]]; then
    chmod 600 "$KEY_FILE"
    success "Keyfile created at $KEY_FILE"
    warn "Back up $KEY_FILE — losing it makes your stored API key unrecoverable."
  fi
fi

# ── First-time config prompts ─────────────────────────────────────────────────
if [[ "$REINSTALL" == false ]]; then
  header "Agent Configuration"
  echo -e "  ${CYAN}Let's configure the agent. Press Enter to skip any field.${NC}"
  echo ""

  read -rp "  Game ID        [default: 1]: " INPUT_GAME
  INPUT_GAME="${INPUT_GAME:-1}"
  "$VENV_PYTHON" "$SCRIPT_DIR/trading_agent.py" config set game_id "$INPUT_GAME"
  success "Game ID set to $INPUT_GAME"

  read -rp "  Username       [optional]:   " INPUT_USER
  if [[ -n "$INPUT_USER" ]]; then
    "$VENV_PYTHON" "$SCRIPT_DIR/trading_agent.py" config set username "$INPUT_USER"
    success "Username set to $INPUT_USER"
  fi

  read -rp "  Base URL       [default: https://stocks.namoh.net]: " INPUT_URL
  INPUT_URL="${INPUT_URL:-https://stocks.namoh.net}"
  "$VENV_PYTHON" "$SCRIPT_DIR/trading_agent.py" config set base_url "$INPUT_URL"
  success "Base URL set to $INPUT_URL"

  echo ""
  echo -e "  ${CYAN}Enter your API key (input hidden, stored encrypted):${NC}"
  read -rsp "  API Key: " INPUT_KEY
  echo ""
  if [[ -n "$INPUT_KEY" ]]; then
    echo "$INPUT_KEY" | "$VENV_PYTHON" -c "
import sys, db
db.init_db()
db.config_set('api_key', sys.stdin.read().strip())
print('  saved')
" && success "API key saved (encrypted)" || warn "API key not saved — set it later in the web UI Config page"
  else
    warn "No API key entered — set it later in the web UI Config page"
  fi

  # ── Schedule setup ─────────────────────────────────────────────────────────
  header "Schedule Setup"
  echo -e "  ${CYAN}When should the agent run automatically? (24-hour local time)${NC}"
  echo "  You can add more times later in the web UI."
  echo ""
  echo "  Suggested: 09:35 (market open), 13:00 (midday), 15:45 (near close)"
  echo "  Press Enter with no input to skip scheduling for now."
  echo ""

  while true; do
    read -rp "  Add a run time HH:MM (or Enter to finish): " INPUT_TIME
    [[ -z "$INPUT_TIME" ]] && break
    if [[ "$INPUT_TIME" =~ ^[0-2][0-9]:[0-5][0-9]$ ]]; then
      "$VENV_PYTHON" "$SCRIPT_DIR/trading_agent.py" schedule add "$INPUT_TIME" &>/dev/null
      success "Scheduled run added: $INPUT_TIME"
    else
      warn "Invalid format — use HH:MM (e.g. 09:35)"
    fi
  done
fi

# ── Systemd service ───────────────────────────────────────────────────────────
header "Setting Up System Service"

if [[ "$REINSTALL" == true ]]; then
  info "Stopping existing service..."
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
fi

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Stock Trading Agent Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${VENV_PYTHON} ${SCRIPT_DIR}/web.py --host 0.0.0.0 --port ${PORT}
WorkingDirectory=${SCRIPT_DIR}
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
User=${PI_USER}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" &>/dev/null
systemctl start  "$SERVICE_NAME"

# Brief pause to let Flask start
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
  success "Service is running"
else
  warn "Service may not have started. Check with:"
  echo "    sudo journalctl -u $SERVICE_NAME -n 30"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
HOSTNAME=$(hostname)

echo ""
divider
echo -e "${BOLD}${GREEN}  Installation complete!${NC}"
divider
echo ""
echo -e "  ${BOLD}Web UI${NC}"
echo -e "    ${CYAN}http://${HOSTNAME}.local:${PORT}${NC}"
if [[ -n "$LOCAL_IP" ]]; then
  echo -e "    ${CYAN}http://${LOCAL_IP}:${PORT}${NC}"
fi
echo ""
echo -e "  ${BOLD}Service management${NC}"
echo "    sudo systemctl status  $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "    sudo systemctl stop    $SERVICE_NAME"
echo ""
echo -e "  ${BOLD}Live service logs${NC}"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo ""

if [[ "$REINSTALL" == false ]]; then
  echo -e "  ${BOLD}Next steps${NC}"
  echo "    1. Open the web UI in your browser"
  if ! "$VENV_PYTHON" -c "import db; db.init_db(); exit(0 if db.config_get('api_key') else 1)" 2>/dev/null; then
    echo "    2. Go to Config and enter your API key"
    echo "    3. Go to Schedule and set your run times"
    echo "    4. Click 'Run Agent Now' to test"
  else
    echo "    2. Go to Schedule to verify your run times"
    echo "    3. Click 'Run Agent Now' on the Dashboard to test"
  fi
fi

echo ""
divider
echo ""
