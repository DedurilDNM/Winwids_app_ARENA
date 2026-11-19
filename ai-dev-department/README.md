# AI Dev Department

Client-server application to manage a team of AI agents working on a shared project.
The end user installs a Windows `.exe`, logs in, registers N accounts on an external
AI service (name/URL live in `.env`, never in code), and starts a "Project" with a task.
The orchestrator model decomposes the task, dispatches subtasks to agents with roles,
agents commit code and statuses through the service's GitHub connector, and the
orchestrator assembles the final result.

## Layout

```
ai-dev-department/
├── server/     # FastAPI + SQLAlchemy async + SQLite (MVP), arq + redis queue
├── client/     # PySide6 (Windows) — Phase 4
├── deploy/     # systemd units + install.sh for Debian 13 — Phase 5
├── DECISIONS.md
└── README.md
```

## Quick start (server)

See [server/README.md](server/README.md). In short:

```bash
cd ai-dev-department/server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # first run auto-generates FERNET_KEY and JWT_SECRET
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/health and /docs. The first run seeds `admin@local`
(password from `ADMIN_PASSWORD`, or a random one-time password printed to stdout).
Create single-use invite codes with `python -m app.admin create-invite`.

## Phases

- Phase 1 — skeleton: config, DB, models, auth (JWT + invite-only registration), health ✅
- Phase 2 — accounts API + VNC manual-login flow (Camoufox / xvfb / x11vnc / noVNC) ✅
  (см. [README_phase2.md](README_phase2.md); графическая приёмка — на Debian через deploy/install.sh)
- Phase 3 — projects API + orchestrator + github watcher + WebSocket statuses
- Phase 4 — PySide6 Windows client + PyInstaller build
- Phase 5 — systemd deployment + Cloudflare Tunnel on clean Debian 13

See DECISIONS.md for every default decision taken without asking.
