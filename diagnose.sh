#!/bin/bash
# =============================================================================
#  Stock Trading Agent — Diagnostics
#  Run any time to check what's working and what isn't.
#  Usage: bash diagnose.sh
# =============================================================================

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()     { echo -e "  ${GREEN}[PASS]${NC} $*"; }
fail()   { echo -e "  ${RED}[FAIL]${NC} $*"; FAILURES=$((FAILURES+1)); }
warn()   { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
info()   { echo -e "  ${CYAN}[INFO]${NC} $*"; }
header() { echo -e "\n${BOLD}${BLUE}--- $* ---${NC}"; }

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="trading-agent-web"
PYTHON="python3"
FAILURES=0

echo ""
echo -e "${BOLD}${BLUE}  Stock Trading Agent — Diagnostic Report${NC}"
echo -e "  $(date)"
echo -e "  Install dir: $INSTALL_DIR"

# ─────────────────────────────────────────────────────────────────────────────
header "1. System Service"
# ─────────────────────────────────────────────────────────────────────────────

if systemctl list-unit-files --quiet "$SERVICE.service" 2>/dev/null | grep -q "$SERVICE"; then
  ok "Service unit file exists"
else
  fail "Service '$SERVICE' is not installed — run: sudo bash install.sh"
fi

if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
  ok "Service is enabled (starts on boot)"
else
  warn "Service is not enabled — run: sudo systemctl enable $SERVICE"
fi

SVC_STATUS=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
if [[ "$SVC_STATUS" == "active" ]]; then
  ok "Service is running"
else
  fail "Service is not running (status: $SVC_STATUS)"
  echo ""
  echo -e "  ${YELLOW}Try these to fix it:${NC}"
  echo "    sudo systemctl start $SERVICE"
  echo "    sudo journalctl -u $SERVICE -n 30 --no-pager"
fi

# Show the ExecStart line so we know the port
EXEC_START=$(systemctl show "$SERVICE" --property=ExecStart 2>/dev/null \
  | grep -oP '(?<=argv\[\]=).*' | head -1 || true)
if [[ -n "$EXEC_START" ]]; then
  info "ExecStart: $EXEC_START"
fi

# Pull port from the service definition
SVC_PORT=$(systemctl show "$SERVICE" --property=ExecStart 2>/dev/null \
  | grep -oP '(?<=--port )\d+' || echo "5000")
info "Configured port: $SVC_PORT"

# ─────────────────────────────────────────────────────────────────────────────
header "2. Network & Port"
# ─────────────────────────────────────────────────────────────────────────────

# Show all IPs
echo ""
info "Network interfaces:"
ip -brief addr 2>/dev/null | grep -v "^lo" | sed 's/^/       /' \
  || ifconfig 2>/dev/null | grep -E "^[a-z]|inet " | grep -v "127.0.0.1" | sed 's/^/       /'

echo ""
# Check if port is listening
if ss -tlnp 2>/dev/null | grep -q ":${SVC_PORT}"; then
  ok "Port $SVC_PORT is open and listening"
  LISTENER=$(ss -tlnp 2>/dev/null | grep ":${SVC_PORT}" | awk '{print $NF}')
  info "Listener: $LISTENER"
else
  fail "Nothing is listening on port $SVC_PORT"
  echo ""
  echo -e "  ${YELLOW}The service may have crashed. Check logs:${NC}"
  echo "    sudo journalctl -u $SERVICE -n 50 --no-pager"
fi

# Test HTTP response locally
if curl -sf --max-time 5 "http://127.0.0.1:${SVC_PORT}/" > /dev/null 2>&1; then
  ok "Web UI responds on http://127.0.0.1:${SVC_PORT}/"
else
  fail "Web UI did not respond on http://127.0.0.1:${SVC_PORT}/"
  echo ""
  echo -e "  ${YELLOW}Possible reasons:${NC}"
  echo "    • Service crashed after starting"
  echo "    • Flask threw a startup error (check logs below)"
  echo "    • Firewall blocking local loopback (unlikely)"
fi

# Firewall check
if command -v ufw &>/dev/null; then
  UFW_STATUS=$(ufw status 2>/dev/null | head -1)
  if echo "$UFW_STATUS" | grep -q "inactive"; then
    ok "ufw firewall is inactive (port not blocked)"
  else
    info "ufw status: $UFW_STATUS"
    if ufw status 2>/dev/null | grep -q "${SVC_PORT}"; then
      ok "ufw has a rule for port $SVC_PORT"
    else
      warn "ufw is active but port $SVC_PORT may not be allowed"
      echo "    Fix: sudo ufw allow ${SVC_PORT}/tcp"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
header "3. Python & Dependencies"
# ─────────────────────────────────────────────────────────────────────────────

if command -v $PYTHON &>/dev/null; then
  PY_VER=$($PYTHON --version 2>&1)
  ok "Python: $PY_VER"
else
  fail "python3 not found"
fi

$PYTHON - 2>&1 <<'PYCHECK'
import sys
results = {}
for mod in ("requests", "flask", "cryptography", "sqlite3"):
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "ok")
        results[mod] = ver
    except ImportError as e:
        results[mod] = f"MISSING: {e}"

for mod, status in results.items():
    if "MISSING" in str(status):
        print(f"  \033[0;31m[FAIL]\033[0m   {mod}: {status}")
    else:
        print(f"  \033[0;32m[PASS]\033[0m   {mod}: {status}")
PYCHECK

# ─────────────────────────────────────────────────────────────────────────────
header "4. Project Files"
# ─────────────────────────────────────────────────────────────────────────────

for f in trading_agent.py web.py db.py requirements.txt; do
  if [[ -f "$INSTALL_DIR/$f" ]]; then
    SIZE=$(du -h "$INSTALL_DIR/$f" | cut -f1)
    ok "$f  ($SIZE)"
  else
    fail "$f is missing"
  fi
done

for d in templates; do
  if [[ -d "$INSTALL_DIR/$d" ]]; then
    COUNT=$(find "$INSTALL_DIR/$d" -name "*.html" | wc -l)
    ok "templates/  ($COUNT HTML files)"
  else
    fail "templates/ directory is missing"
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
header "5. Database & Config"
# ─────────────────────────────────────────────────────────────────────────────

DB="$INSTALL_DIR/trading_agent.db"
if [[ -f "$DB" ]]; then
  SIZE=$(du -h "$DB" | cut -f1)
  ok "Database exists ($SIZE)"
else
  warn "Database not found — will be created on first run"
fi

KEY="$INSTALL_DIR/.keyfile"
if [[ -f "$KEY" ]]; then
  ok ".keyfile exists"
  KPERMS=$(stat -c "%a" "$KEY" 2>/dev/null || stat -f "%A" "$KEY" 2>/dev/null)
  [[ "$KPERMS" == "600" ]] && ok ".keyfile permissions are 600 (secure)" \
                            || warn ".keyfile permissions are $KPERMS (should be 600)"
else
  warn ".keyfile not found — API key encryption won't work until it's created"
fi

# Show config (masked)
if [[ -f "$DB" ]]; then
  echo ""
  info "Current configuration:"
  cd "$INSTALL_DIR"
  $PYTHON trading_agent.py config show 2>/dev/null | sed 's/^/       /' || warn "Could not read config"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "6. Recent Service Logs"
# ─────────────────────────────────────────────────────────────────────────────

echo ""
if systemctl list-unit-files --quiet "$SERVICE.service" 2>/dev/null | grep -q "$SERVICE"; then
  journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null \
    | tail -30 \
    | sed 's/^/  /' \
    | grep --color=never -E "(ERROR|error|Traceback|Exception|WARN|warn|Started|start|stop|failed)" \
    || journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null | tail -20 | sed 's/^/  /'
else
  info "Service not installed — no logs available"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "Summary"
# ─────────────────────────────────────────────────────────────────────────────

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
if [[ $FAILURES -eq 0 ]]; then
  echo -e "  ${GREEN}${BOLD}All checks passed.${NC}"
  echo ""
  echo -e "  Web UI should be accessible at:"
  echo -e "    ${CYAN}http://$(hostname).local:${SVC_PORT}${NC}"
  [[ -n "$LOCAL_IP" ]] && echo -e "    ${CYAN}http://${LOCAL_IP}:${SVC_PORT}${NC}"
else
  echo -e "  ${RED}${BOLD}$FAILURES check(s) failed.${NC}"
  echo ""
  echo "  Quick fixes to try:"
  echo "    sudo systemctl restart $SERVICE          # restart the service"
  echo "    sudo journalctl -u $SERVICE -n 50 --no-pager  # full log"
  echo "    sudo bash install.sh                     # re-run installer"
fi
echo ""
