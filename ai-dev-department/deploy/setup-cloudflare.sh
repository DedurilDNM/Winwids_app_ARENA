#!/bin/bash
# Permanent public HTTPS address via a Cloudflare Tunnel (no open ports needed).
# Before: a domain on Cloudflare + a tunnel whose Public Hostname points to
# http://localhost:8080 (see the steps the assistant gave / deploy/README.md).
#
#   sudo bash deploy/setup-cloudflare.sh aidd.example.dpdns.org
#
# It asks for the tunnel token without showing it (paste the token or the whole
# "cloudflared service install <token>" command from the dashboard).
# What it does:
#   1. installs cloudflared if missing;
#   2. stores the token in /etc/cloudflared/ai-dept.env (root only, chmod 600);
#   3. Caddy on 127.0.0.1:8080 (local only): / -> app, /novnc/ -> agents' browsers;
#   4. systemd service "cloudflared-aidd" (starts on boot);
#   5. NOVNC_PUBLIC_BASE_URL in server/.env, restarts the app (service aidd).
set -euo pipefail

HOST="${1:-}"
[ -n "$HOST" ] || { echo "Usage: sudo bash $0 <hostname, e.g. aidd.example.dpdns.org>"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
HOST="${HOST#https://}"; HOST="${HOST%%/*}"

SERVER_DIR="$(cd "$(dirname "$0")/../server" && pwd)"
ENV_FILE="$SERVER_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE"; exit 1; }

echo "== 1. cloudflared"
CF="$(command -v cloudflared || true)"
if [ -z "$CF" ]; then
  case "$(dpkg --print-architecture)" in
    amd64) DEB=cloudflared-linux-amd64.deb ;;
    arm64) DEB=cloudflared-linux-arm64.deb ;;
    *) echo "Unsupported architecture"; exit 1 ;;
  esac
  curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/$DEB"
  dpkg -i /tmp/cloudflared.deb >/dev/null
  rm -f /tmp/cloudflared.deb
  CF="$(command -v cloudflared)"
fi
echo "   $("$CF" --version 2>&1 | head -1)"

echo "== 2. Tunnel token"
install -d -m 700 /etc/cloudflared
if [ -s /etc/cloudflared/ai-dept.env ] && [ "${NEW_TOKEN:-0}" != 1 ]; then
  echo "   already saved (to replace it: NEW_TOKEN=1 sudo -E bash $0 $HOST)"
else
  read -rsp "   Paste the tunnel token (input is hidden) and press Enter: " RAW; echo
  # accept the bare token or the whole "cloudflared service install <token>" command
  TOKEN="$(printf '%s' "$RAW" | tr -s ' \t' '\n' | awk 'length($0) > 60' | tail -1)"
  unset RAW
  [ -n "$TOKEN" ] || { echo "   That does not look like a tunnel token (a long string starting with eyJ)."; exit 1; }
  umask 077
  printf 'TUNNEL_TOKEN=%s\n' "$TOKEN" > /etc/cloudflared/ai-dept.env
  unset TOKEN
  chmod 600 /etc/cloudflared/ai-dept.env
  echo "   saved to /etc/cloudflared/ai-dept.env"
fi

echo "== 3. Caddy (local router on 127.0.0.1:8080)"
if ! command -v caddy >/dev/null; then
  apt-get update -qq && apt-get install -y -qq caddy
fi
cat > /etc/caddy/Caddyfile <<'EOF'
# Local only: the Cloudflare Tunnel publishes it with HTTPS (deploy/setup-cloudflare.sh).
http://127.0.0.1:8080, http://localhost:8080 {
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

echo "== 4. Tunnel service"
pkill -f "cloudflared tunnel --url" 2>/dev/null || true   # old quick tunnels
cat > /etc/systemd/system/cloudflared-aidd.service <<EOF
[Unit]
Description=Cloudflare Tunnel (AI Dev Department)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
DynamicUser=yes
# the token is read from the environment, so it is not visible in the process list
EnvironmentFile=/etc/cloudflared/ai-dept.env
ExecStart=$CF --no-autoupdate tunnel --metrics 127.0.0.1:20241 run
Restart=on-failure
RestartSec=5
TimeoutStartSec=0
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q cloudflared-aidd
systemctl restart cloudflared-aidd

echo "== 5. server/.env + restart"
set_env() {  # set_env KEY VALUE (replace or append)
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
set_env NOVNC_PUBLIC_BASE_URL "https://$HOST/novnc"
set_env CORS_ORIGINS "https://$HOST"
if systemctl cat aidd >/dev/null 2>&1; then
  systemctl restart aidd
else
  echo "   Restart the app yourself so it reads the new .env (service aidd not found)."
fi

echo "== 6. Check"
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8080/health >/dev/null && echo "   app via Caddy: OK" || echo "   app via Caddy: NOT OK (see $SERVER_DIR/data/uvicorn.log)"
systemctl is-active -q cloudflared-aidd && echo "   tunnel service: running" || echo "   tunnel service: NOT running (journalctl -u cloudflared-aidd -n 30)"
OK=0
for i in $(seq 1 12); do
  if curl -fsS --max-time 10 "https://$HOST/health" >/dev/null 2>&1; then OK=1; break; fi
  sleep 5
done
if [ "$OK" = 1 ]; then
  echo "   https://$HOST : OK"
  echo
  echo "Done. Open https://$HOST/app/ on any device (install it as an app from the browser menu)."
else
  echo "   https://$HOST : not reachable yet. Check in the Cloudflare dashboard: the domain is Active,"
  echo "   the tunnel is HEALTHY, and its Public Hostname is $HOST -> HTTP localhost:8080."
  echo "   Tunnel log: journalctl -u cloudflared-aidd -n 30 --no-pager"
fi
