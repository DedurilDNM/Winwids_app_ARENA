#!/bin/bash
# Permanent HTTPS address WITHOUT open ports (server behind NAT): Tailscale.
# The app is reachable at https://<machine>.<tailnet>.ts.net from your own
# devices logged in to the same Tailscale account (phone, PC). One-time:
#
#   sudo bash deploy/setup-tailscale.sh
#
# What it does:
#   1. installs Tailscale and logs this server in (prints a link to open once);
#   2. Caddy on 127.0.0.1:$LOCAL_PORT (local only): / -> app, /novnc/ -> agents' browsers;
#   3. `tailscale serve` publishes it with HTTPS inside your tailnet;
#   4. sets NOVNC_PUBLIC_BASE_URL in server/.env and restarts the app (service aidd,
#      created by deploy/setup-domain.sh; started here if it exists).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

SERVER_DIR="$(cd "$(dirname "$0")/../server" && pwd)"
ENV_FILE="$SERVER_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE"; exit 1; }

# Local port for Caddy (must be free; another program may use 8080).
LOCAL_PORT="${LOCAL_PORT:-8787}"
if ss -ltn "sport = :$LOCAL_PORT" | grep -q LISTEN && ! ss -ltnp "sport = :$LOCAL_PORT" | grep -q caddy; then
  echo "Port $LOCAL_PORT is busy: run again with another one, e.g. LOCAL_PORT=8788 sudo -E bash $0 ${1:-}"; exit 1
fi

echo "== 1. Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable -q --now tailscaled
if ! tailscale status >/dev/null 2>&1; then
  echo "   Open the link below and log in (the same account you will use on the phone/PC):"
  tailscale up
fi
NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
[ -n "$NAME" ] || { echo "Could not read the Tailscale name of this machine"; exit 1; }
echo "   this server: $NAME"

echo "== 2. Caddy (local router on 127.0.0.1:$LOCAL_PORT)"
if ! command -v caddy >/dev/null; then
  apt-get update -qq && apt-get install -y -qq caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
# Local only: Tailscale serves it with HTTPS (deploy/setup-tailscale.sh).
# any Host name (Tailscale passes the public one); listens on 127.0.0.1 only
http://:$LOCAL_PORT {
	bind 127.0.0.1
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

echo "== 3. tailscale serve (HTTPS inside your tailnet)"
echo "   If it prints a link to enable HTTPS certificates: open it, click Enable, then run this script again."
tailscale serve --bg http://127.0.0.1:$LOCAL_PORT

echo "== 4. server/.env + restart"
set_env() {  # set_env KEY VALUE (replace or append)
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
set_env NOVNC_PUBLIC_BASE_URL "https://$NAME/novnc"
set_env CORS_ORIGINS "https://$NAME"
if systemctl cat aidd >/dev/null 2>&1; then
  systemctl restart aidd
else
  echo "   Restart the app yourself so it reads the new .env (service aidd not found)."
fi

echo "== 5. Check"
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:$LOCAL_PORT/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:$LOCAL_PORT/health >/dev/null && echo "   app via Caddy: OK" || echo "   app via Caddy: NOT OK (see $SERVER_DIR/data/uvicorn.log)"
tailscale serve status || true
echo
echo "Done. On the phone and PC: install Tailscale, log in with the same account, open:"
echo "   https://$NAME/app/"
