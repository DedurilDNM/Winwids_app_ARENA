#!/bin/bash
# The app as a systemd service "aidd" + log rotation. Idempotent, safe to re-run.
#
#   sudo bash deploy/install-service.sh
#
# - aidd.service: starts on boot, restarts on crash, exactly ONE uvicorn worker
#   (browsers and VNC sessions live in that process); runs as the owner of server/.
# - /etc/logrotate.d/aidd: data/uvicorn.log is rotated daily or at 20 MB, 7 kept.
# Projects that were running when the service restarts continue by themselves
# (AUTO_RESUME_INTERRUPTED, on by default).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

SERVER_DIR="$(cd "$(dirname "$0")/../server" && pwd)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$SERVER_DIR")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
mkdir -p "$SERVER_DIR/data/tmp"
chown "$RUN_USER" "$SERVER_DIR/data" "$SERVER_DIR/data/tmp"

cat > /etc/systemd/system/aidd.service <<EOF
[Unit]
Description=AI Dev Department (web app + agents)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$SERVER_DIR
Environment=HOME=$RUN_HOME
Environment=PYTHONUNBUFFERED=1
# temp files (browser profiles, ~100 MB each) on the main disk, not on a small /tmp
Environment=TMPDIR=$SERVER_DIR/data/tmp
# exactly ONE worker: browsers and VNC sessions live in this process
ExecStart=$SERVER_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header
StandardOutput=append:$SERVER_DIR/data/uvicorn.log
StandardError=append:$SERVER_DIR/data/uvicorn.log
KillMode=mixed
TimeoutStopSec=30
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/logrotate.d/aidd <<EOF
$SERVER_DIR/data/uvicorn.log {
    daily
    maxsize 20M
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

# an app started by hand (nohup) must not hold port 8000
if ! systemctl is-active -q aidd; then
  pkill -u "$RUN_USER" -f "uvicorn app.main:app" || true
  sleep 2
fi
systemctl daemon-reload
systemctl enable -q aidd
systemctl restart aidd
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 1; done
if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "aidd: running; logs rotate via /etc/logrotate.d/aidd"
else
  echo "aidd: NOT running - see $SERVER_DIR/data/uvicorn.log"
fi
