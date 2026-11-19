# AI Dev Department — Server

FastAPI + SQLAlchemy 2.0 async + SQLite (MVP, PostgreSQL-ready) + arq/redis queue.
Python 3.12+ (3.13 on Debian 13).

## First run

```bash
cd ai-dev-department/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set SERVICE_URL (defaults to https://example.com for tests),
# PROXY_URL, and optionally ADMIN_PASSWORD.
# FERNET_KEY and JWT_SECRET are auto-generated into .env on first start.
chmod 600 .env
```

## Run

```bash
uvicorn app.main:app --reload          # dev
uvicorn app.main:app --host 0.0.0.0 --port 8000   # bind for tunnel/LAN
```

On startup the app:

1. Auto-generates `FERNET_KEY` / `JWT_SECRET` into `.env` if missing (chmod 600).
2. Creates the DB schema (SQLite at `server/data/app.db` by default).
3. Seeds `admin@local` if the users table is empty. Password comes from
   `ADMIN_PASSWORD`, or a random one-time password printed to stdout **once**.

## Phase 2 — accounts & VNC login

See [../README_phase2.md](../README_phase2.md) for the full guide. Quick reference:

```
POST   /api/accounts                       # create (pending_login) + start VNC session
POST   /api/accounts/{id}/vnc/start        # (re)start VNC session -> vnc_url
POST   /api/accounts/{id}/vnc/stop         # close noVNC path, browser stays alive
POST   /api/accounts/{id}/confirm-login    # "Готово" -> encrypted storage_state, status active
GET    /api/accounts                       # list (max 10 per user)
PATCH  /api/accounts/{id}                  # edit name/role/repos/status(disabled)
DELETE /api/accounts/{id}                  # stop session + browser, 204
```

System deps for the VNC stack are installed by `deploy/install.sh`
(xvfb, x11vnc, novnc, websockify, redis-server). Without them the API still works;
VNC endpoints degrade to explicit 503/`vnc_error` responses.

Tests: `pip install -r requirements-dev.txt && ./.venv/bin/python -m pytest tests/ -v`
(9 tests, fake Camoufox, no display needed).

## Acceptance checks

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}

# Interactive docs
xdg-open http://127.0.0.1:8000/docs

# Create an invite (admin CLI)
python -m app.admin create-invite

# Register a user with the invite
curl -s -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"correct horse battery staple","invite_code":"<code>"}'

# Login
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@local","password":"<admin password>"}'
# -> {"access_token":"...","refresh_token":"...","token_type":"bearer"}

# Authenticated call
curl -s http://127.0.0.1:8000/api/auth/me -H "Authorization: Bearer <access_token>"
```

## Database and migrations

MVP uses `create_all` on startup. Alembic is pre-wired for when the schema
evolves:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

`alembic/env.py` reads `DATABASE_URL` from the app config (sync driver).

## PostgreSQL migration path

1. `pip install asyncpg psycopg2-binary`
2. Set `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname`
3. No model code changes required (JSON columns map to `JSONB`-compatible types).

## Notes

- Passwords: bcrypt via passlib (`bcrypt==4.0.1` pin — passlib 1.7.4 breaks on bcrypt ≥ 4.1).
- Tokens: JWT access (15 min) + refresh (30 days). Refresh tokens are stateless; the
  client keeps them in Windows Credential Manager via `keyring`.
- The external AI service is **never** named in code — only `SERVICE_URL`,
  `SERVICE_DISPLAY_NAME` and `ADAPTER_MODULE`. The default adapter is an
  intentionally failing stub (`NotImplementedError`), per the MVP contract.
- Logs never contain bodies; query params named like secrets are masked.
