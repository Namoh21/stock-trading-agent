#!/bin/bash
# Run once on the Pi to install dependencies and wire up the systemd service.
# Usage: sudo bash install.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT="$SCRIPT_DIR/trading_agent.py"
SERVICE_NAME="trading-agent"
PI_USER="${SUDO_USER:-pi}"   # preserve the non-root user who ran sudo

echo "==> Installing Python dependencies…"
pip3 install -r "$SCRIPT_DIR/requirements.txt"

echo "==> Initialising database and encryption key…"
# Run as the real user so .keyfile and trading_agent.db are owned correctly
sudo -u "$PI_USER" python3 "$AGENT" config show || true

echo ""
echo "==> First-time setup (run these as $PI_USER):"
echo "    python3 $AGENT config set-api-key"
echo "    python3 $AGENT config set game_id 1"
echo "    python3 $AGENT config set username yourname"
echo "    python3 $AGENT config set base_url https://stocks.namoh.net"
echo ""
echo "==> Configure run schedule (e.g. market open + midday):"
echo "    python3 $AGENT schedule add 09:35"
echo "    python3 $AGENT schedule add 13:00"
echo ""

# ── systemd service ────────────────────────────────────────────────────────────
echo "==> Writing systemd service…"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Stock Trading Agent Scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${AGENT} schedule run
WorkingDirectory=${SCRIPT_DIR}
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
User=${PI_USER}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl start  ${SERVICE_NAME}

echo ""
echo "==> Service started!"
echo "    Status : sudo systemctl status ${SERVICE_NAME}"
echo "    Logs   : sudo journalctl -u ${SERVICE_NAME} -f"
echo "    DB logs: python3 ${AGENT} logs --tail 50"
echo "    History: python3 ${AGENT} history"
