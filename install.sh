#!/bin/bash
# Run once on the Pi to install dependencies and wire up systemd services.
# Usage: sudo bash install.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_USER="${SUDO_USER:-pi}"

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
ExecStart=/usr/bin/python3 ${SCRIPT_DIR}/web.py --host 0.0.0.0 --port 5000
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
echo "    Web UI : http://$(hostname -I | awk '{print $1}'):5000"
echo "    Status : sudo systemctl status trading-agent-web"
echo "    Logs   : sudo journalctl -u trading-agent-web -f"
