#!/bin/bash
# (Re)start the e2e stack: fake service (:9100) + the app with fake GitHub (:8000),
# real Xvfb/noVNC. Keeps the DB unless FRESH=1. Then run a stage, e.g.:
#
#   FRESH=1 web/tests/e2e-server.sh
#   NODE_PATH=$(npm root -g) ADMIN_PASSWORD=adminpass123 node web/tests/e2e-stage11.mjs
#
# Env: E2E_SERVICE_URL (fake service URL + mode flags), PYTHON (default: server/.venv
# or python3), E2E_STATE (state dir, default /tmp/aidd-e2e), ADAPTER_* overrides.
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$(cd "$HERE/../../server" && pwd)"
S="${E2E_STATE:-/tmp/aidd-e2e}"; [ "$FRESH" = 1 ] && rm -rf "$S"; mkdir -p "$S"
PY="${PYTHON:-$([ -x "$SERVER/.venv/bin/python" ] && echo "$SERVER/.venv/bin/python" || echo python3)}"
pkill -f "e2e_server.py"; pkill -f "uvicorn app.main:app"; pkill -f websockify; pkill x11vnc; pkill Xvfb; sleep 1
pgrep -f "tests/fake_service.py" >/dev/null || (nohup python3 "$HERE/fake_service.py" 9100 "$S/service.log" >/dev/null 2>&1 &)
cd "$SERVER" || exit 1
export DATABASE_URL="sqlite+aiosqlite:///$S/app.db" VNC_HEADLESS=false REDIS_URL=redis://127.0.0.1:1/0 \
  CI_POLL_SECONDS=1 CI_START_TIMEOUT_SECONDS=20 ADMIN_PASSWORD=adminpass123 PROXY_URL= \
  SERVICE_URL="${E2E_SERVICE_URL:-http://127.0.0.1:9100/}" \
  NOVNC_PUBLIC_BASE_URL=http://127.0.0.1:6080 NOVNC_WEB_ROOT=/usr/share/novnc \
  E2E_CHROMIUM="${E2E_CHROMIUM:-/opt/pw-browsers/chromium-1243/chrome-linux64/chrome}" E2E_FAKE_GITHUB=1
nohup "$PY" "$HERE/e2e_server.py" >> "$S/srv.log" 2>&1 &
for i in $(seq 1 40); do curl -sf 127.0.0.1:8000/health >/dev/null && echo up && exit 0; sleep 0.5; done
echo FAILED; tail -20 "$S/srv.log"; exit 1
