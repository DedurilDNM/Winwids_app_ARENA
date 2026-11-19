#!/usr/bin/env bash
# AI Dev Department - server bootstrap for Debian (12/13).
# Installs system dependencies (Xvfb, x11vnc, noVNC, websockify, redis),
# creates the Python virtualenv, installs requirements and downloads the
# Camoufox browser binary. Idempotent: safe to re-run.
#
# Usage (as root or via sudo):
#   sudo bash deploy/install.sh
#
# Phase 2 scope: system + Python dependencies only. systemd units and the
# Cloudflare Tunnel are handled in Phase 5 (this script prints hints).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root (use sudo)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$REPO_ROOT/server"

echo "==> apt: base packages"
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  git curl ca-certificates \
  redis-server \
  xvfb x11vnc novnc websockify \
  fonts-liberation fonts-dejavu-core \
  sqlite3

echo "==> sanity: display/VNC tooling"
command -v Xvfb     >/dev/null && echo "  Xvfb:      $(command -v Xvfb)"
command -v x11vnc   >/dev/null && echo "  x11vnc:    $(command -v x11vnc)"
command -v websockify >/dev/null && echo "  websockify:$(command -v websockify)" || true
if [[ -d /usr/share/novnc ]]; then
  echo "  noVNC:     /usr/share/novnc"
else
  echo "  noVNC:     /usr/share/novnc MISSING - trying pip websockify+git checkout"
  git clone --depth 1 https://github.com/novnc/noVNC /usr/share/novnc || true
fi

echo "==> python virtualenv at $SERVER_DIR/.venv"
cd "$SERVER_DIR"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> camoufox browser binary"
# Downloads the browser build matching the installed camoufox package.
./.venv/bin/python -m camoufox fetch || {
  echo "  WARNING: 'camoufox fetch' failed. Check network/GitHub access and re-run." >&2
}

echo "==> .env"
if [[ ! -f "$SERVER_DIR/.env" ]]; then
  cp "$SERVER_DIR/.env.example" "$SERVER_DIR/.env"
  chmod 600 "$SERVER_DIR/.env"
  echo "  Created .env from .env.example (chmod 600). Edit SERVICE_URL/PROXY_URL."
else
  chmod 600 "$SERVER_DIR/.env" || true
  echo "  .env already exists - left untouched."
fi

mkdir -p "$SERVER_DIR/data"
chmod 700 "$SERVER_DIR/data"

echo
echo "==> Done. Next steps:"
echo "  1) cd $SERVER_DIR && edit .env (SERVICE_URL, PROXY_URL, ADMIN_PASSWORD)"
echo "  2) ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo "  3) Verify: curl -s http://127.0.0.1:8000/health"
echo
echo "  Phase 5 will add systemd units (api, worker, cloudflared). For Cloudflare Tunnel:"
echo "    - Install cloudflared from the Cloudflare dashboard (Zero Trust > Tunnels)"
echo "    - Create a public hostname app.<domain> -> http://127.0.0.1:8000"
echo "    - Route /novnc to http://127.0.0.1:6080 (or run behind nginx, see deploy/nginx.conf.example)"
echo "    - Never expose Xvfb/x11vnc ports directly."
