#!/bin/bash
# FreeLLMAPI (github.com/tashfeenahmed/freellmapi, MIT) next to the app: a local router over the
# free tiers of many LLM providers, as the systemd service «freellmapi» on 127.0.0.1:3001 (not
# reachable from outside). In the app it is the aggregator «FreeLLMAPI»: base URL preset, the key
# is its unified key. Idempotent: re-running updates it and keeps its data and encryption key.
#
#   sudo bash deploy/install-freellmapi.sh
#
# Then open its dashboard through an SSH tunnel (ssh -L 3001:127.0.0.1:3001 <server>, then
# http://localhost:3001), add your provider keys on «Keys», copy the unified key «freellmapi-…»
# and add it in the app: Settings → API keys → «FreeLLMAPI (бесплатные тарифы)».
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

DIR="${FREELLMAPI_DIR:-/opt/freellmapi}"
PORT="${FREELLMAPI_PORT:-3001}"
REF="${FREELLMAPI_REF:-main}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$APP_DIR")}"

# Node.js 20.18+ (FreeLLMAPI's requirement)
need_node() {
  command -v node >/dev/null 2>&1 || return 0
  node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>20||(a===20&&b>=18)?1:0)'
}
if need_node; then
  echo "Installing Node.js 22 (NodeSource)…"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
echo "Node $(node --version)"

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q --depth 1 origin "$REF" && git -C "$DIR" reset -q --hard FETCH_HEAD
else
  git clone -q --depth 1 --branch "$REF" https://github.com/tashfeenahmed/freellmapi "$DIR"
fi
chown -R "$RUN_USER" "$DIR"

cd "$DIR"
if [ ! -f .env ]; then
  KEY="$(node -e 'console.log(require("crypto").randomBytes(32).toString("hex"))')"
  printf "ENCRYPTION_KEY=%s\nPORT=%s\nHOST=127.0.0.1\nNODE_ENV=production\n" "$KEY" "$PORT" > .env
  chmod 600 .env
  chown "$RUN_USER" .env
  echo "Created $DIR/.env (keep it: it holds the encryption key of the stored provider keys)"
fi
sudo -u "$RUN_USER" npm ci --no-audit --no-fund --loglevel=error
sudo -u "$RUN_USER" npm run build --silent

cat > /etc/systemd/system/freellmapi.service <<EOF
[Unit]
Description=FreeLLMAPI (router over free LLM tiers) for AIDD
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
Environment=HOST=127.0.0.1
ExecStart=$(command -v node) server/dist/index.js
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now freellmapi
systemctl restart freellmapi
for i in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break; sleep 1; done
if curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
  echo "FreeLLMAPI is up on 127.0.0.1:$PORT"
  echo "Dashboard: ssh -L $PORT:127.0.0.1:$PORT <this server>, then open http://localhost:$PORT"
else
  echo "FreeLLMAPI did not answer; see: journalctl -u freellmapi -n 50 --no-pager"
  exit 1
fi
