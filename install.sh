#!/bin/bash
# Run once on the Pi to install dependencies and wire up systemd services.
# Usage:
#   sudo bash install.sh               # prompts for port (default 5000)
#   sudo bash install.sh --port 8080   # set port non-interactively

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_USER="${SUDO_USER:-pi}"

# ── Port selection ─────────────────────────────────────────────────────────────
PORT=""

# Parse --port argument if provided
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port|-p)
      PORT="$2"; shift 2 ;;
    --port=*)
      PORT="${1#*=}"; shift ;;
    *)
      echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# Validate a port number
validate_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

# If not supplied via flag, prompt
if [ -z "$PORT" ]; then
  while true; do
    read -rp "Enter web UI port [default: 5000]: " PORT
    PORT="${PORT:-5000}"
    if validate_port "$PORT"; then
      break
    else
      echo "  Invalid port. Enter a number between 1 and 65535."
    fi
  done
else
  if ! validate_port "$PORT"; then
    echo "Invalid port '$PORT'. Must be 1-65535."; exit 1
  fi
fi

echo "==> Web UI will run on port $PORT"

echo "==> Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "==> Initialising database and encryption key..."
sudo -u "$PI_USER" python3 "$SCRIPT_DIR/trading_agent.py" config show || true

echo ""
echo "==> First-time setup (run these as $PI_USER):"
echo "    python3 $SCRIPT_DIR/trading_agent.py config set-api-key"
echo "    python3 $SCRIPT_DIR/trading_agent.py config set game_id 1"
echo "    python3 $SCRIPT_DIR/trading_agent.py config set username yourname"
echo "    python3 $SCRIPT_DIR/trading_agent.py config set base_url https://stocks.namoh.net"
echo ""
echo "==> Then add a schedule and start the web UI:"
echo "    python3 $SCRIPT_DIR/trading_agent.py schedule add 09:35"
echo "    python3 $SCRIPT_DIR/web.py"
echo ""

# ── Web UI systemd service (includes built-in scheduler) ──────────────────────
echo "==> Writing systemd service: trading-agent-web..."
cat > /etc/systemd/system/trading-agent-web.service <<EOF
[Unit]
Description=Stock Trading Agent Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${SCRIPT_DIR}/web.py --host 0.0.0.0 --port ${PORT}
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
systemctl enable trading-agent-web
systemctl start  trading-agent-web

echo ""
echo "==> Done!"
echo "    Web UI : http://$(hostname -I | awk '{print $1}'):${PORT}"
echo "    Status : sudo systemctl status trading-agent-web"
echo "    Logs   : sudo journalctl -u trading-agent-web -f"
