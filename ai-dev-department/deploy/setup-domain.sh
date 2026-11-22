#!/bin/bash
# Permanent address for the app: https://<DOMAIN> (e.g. a DuckDNS name that
# points to this server's public IP). One-time setup, run with sudo:
#
#   sudo bash deploy/setup-domain.sh superagent52.duckdns.org
#
# What it does:
#   1. checks that the domain points to this server;
#   2. installs Caddy - HTTPS certificate (Let's Encrypt) is automatic;
#      /           -> the app (127.0.0.1:8000)
#      /novnc/...  -> the agents' browsers viewer (websockify, 127.0.0.1:6080)
#   3. sets NOVNC_PUBLIC_BASE_URL=https://<DOMAIN>/novnc in server/.env;
#   4. runs the app as a systemd service "aidd" (starts on boot, restarts on crash).
#
# Needs ports 80 and 443 open to the internet (cloud firewall / security group).
# After it: update = git pull + sudo systemctl restart aidd (the address never changes).
set -euo pipefail

DOMAIN="${1:-}"
[ -n "$DOMAIN" ] || { echo "Usage: sudo bash $0 <domain>"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

SERVER_DIR="$(cd "$(dirname "$0")/../server" && pwd)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$SERVER_DIR")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
ENV_FILE="$SERVER_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE - run deploy/install.sh first"; exit 1; }

echo "== 1. DNS"
MY_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
echo "   this server: ${MY_IP:-unknown}, $DOMAIN -> ${DNS_IP:-not found}"
if [ -z "$DNS_IP" ] || { [ -n "$MY_IP" ] && [ "$DNS_IP" != "$MY_IP" ]; }; then
  echo "   The domain must point to this server: on duckdns.org set its IP to ${MY_IP:-the public IP of this server}, wait a minute, run again."
  exit 1
fi

echo "== 2. Caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null; then
  apt-get update -qq && apt-get install -y -qq caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	# agents' browsers (noVNC + websocket); prefix stripped for websockify
	handle_path /novnc/* {
		reverse_proxy 127.0.0.1:6080
	}
	handle {
		reverse_proxy 127.0.0.1:8000
	}
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable -q caddy
systemctl restart caddy

echo "== 3. server/.env"
set_env() {  # set_env KEY VALUE (replace or append)
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
set_env NOVNC_PUBLIC_BASE_URL "https://$DOMAIN/novnc"
set_env CORS_ORIGINS "https://$DOMAIN"
echo "   NOVNC_PUBLIC_BASE_URL=https://$DOMAIN/novnc"

echo "== 4. systemd service aidd (user: $RUN_USER)"
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
# the app started by hand (nohup) must not hold port 8000
pkill -u "$RUN_USER" -f "uvicorn app.main:app" || true
sleep 2
systemctl daemon-reload
systemctl enable -q --now aidd

echo "== 5. Check"
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8000/health >/dev/null && echo "   app: OK" || echo "   app: NOT running - see $SERVER_DIR/data/uvicorn.log"
if curl -fsS --max-time 60 "https://$DOMAIN/health" >/dev/null 2>&1; then
  echo "   https://$DOMAIN : OK"
  echo
  echo "Done. Open https://$DOMAIN/app/  (install it as an app from the browser menu)."
  echo "Quick tunnels (cloudflared --url) are not needed any more: pkill -f 'cloudflared tunnel .*--url'"
else
  echo "   https://$DOMAIN : not reachable yet. Usually ports 80/443 are closed in the cloud firewall"
  echo "   (open TCP 80 and 443 to 0.0.0.0/0), then: sudo systemctl restart caddy ; journalctl -u caddy -n 30"
fi
