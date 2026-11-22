# Deployment (Debian 13)

## Quick: permanent HTTPS address on your own domain (no Cloudflare)

The domain (e.g. a DuckDNS name) must point to the server's public IP, and TCP
ports 80 and 443 must be open. Then, once:

```bash
sudo bash deploy/setup-domain.sh <your-domain>
```

It installs Caddy (automatic Let's Encrypt certificate), proxies `/` to the app
and `/novnc/` to the agents' browsers viewer, sets `NOVNC_PUBLIC_BASE_URL`, and
runs the app as the systemd service `aidd`. Updates afterwards:
`git pull && sudo systemctl restart aidd`.

## Full install

| File | Purpose |
|------|---------|
| `install.sh` | System packages (Xvfb, x11vnc, noVNC, websockify, Redis), venv, `.env` |
| `systemd/ai-dept-api.service` | FastAPI via uvicorn on `127.0.0.1:8000` (**1 worker** — browser/VNC state is in-process) |
| `systemd/ai-dept-worker.service` | arq worker: `run_project` jobs + 5-min session health check |
| `systemd/cloudflared.service` | Cloudflare Tunnel (token in `/etc/cloudflared/ai-dept.env`) |

The units assume the repo at `/opt/ai-dev-department` and a system user
`aidept`. Different path/user → `sudo systemctl edit <unit>` (override) or
edit the copies in `/etc/systemd/system/`.

## Install

```bash
# 1. Code + dependencies
sudo git clone <repo> /opt/ai-dev-department-src
sudo cp -r /opt/ai-dev-department-src/ai-dev-department /opt/ai-dev-department
sudo bash /opt/ai-dev-department/deploy/install.sh

# 2. Service user; it must own data/ and .env (FERNET_KEY/JWT_SECRET are written on first run)
sudo useradd --system --home-dir /opt/ai-dev-department/server/data/home --shell /usr/sbin/nologin aidept
sudo mkdir -p /opt/ai-dev-department/server/data/home
sudo chown -R aidept:aidept /opt/ai-dev-department/server/data /opt/ai-dev-department/server/.env

# 3. Camoufox browser build must live in the service user's HOME
cd /opt/ai-dev-department/server
sudo -u aidept HOME=/opt/ai-dev-department/server/data/home ./.venv/bin/python -m camoufox fetch

# 4. Configure server/.env: SERVICE_URL, PROXY_URL, ADMIN_PASSWORD, ENVIRONMENT=production,
#    NOVNC_PUBLIC_BASE_URL=https://app.<YOUR_DOMAIN>/novnc, CORS_ORIGINS

# 5. Cloudflare Tunnel (cloudflared from https://pkg.cloudflare.com)
sudo install -d -m 700 /etc/cloudflared
echo 'TUNNEL_TOKEN=<PASTE_TUNNEL_TOKEN_HERE>' | sudo tee /etc/cloudflared/ai-dept.env >/dev/null
sudo chmod 600 /etc/cloudflared/ai-dept.env
#    Tunnel public hostnames (dashboard):
#      app.<YOUR_DOMAIN>          -> http://127.0.0.1:8000
#      app.<YOUR_DOMAIN>/novnc/*  -> http://127.0.0.1:6080

# 6. Units
sudo cp /opt/ai-dev-department/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now redis-server ai-dept-api ai-dept-worker cloudflared
```

## Check

```bash
systemctl status ai-dept-api ai-dept-worker cloudflared
curl -s http://127.0.0.1:8000/health          # {"status":"ok"}
journalctl -u ai-dept-api -f                  # logs (secrets are masked)
sudo -u aidept /opt/ai-dev-department/server/.venv/bin/python -m app.admin create-invite
```

Validate unit files after editing: `systemd-analyze verify /etc/systemd/system/ai-dept-*.service`.

Never expose Xvfb/x11vnc (59xx) or websockify (6080) ports directly — only
through the tunnel.
