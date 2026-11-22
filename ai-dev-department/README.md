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

## Надёжность (калибровка, проверка перед запуском)

- **«Калибровка»** у аккаунта: сервер отправляет одно тестовое сообщение и сам
  выясняет, где на странице сервиса ответ и как видно, что сервис ещё пишет.
  Результат хранится отдельно для каждого аккаунта и раздела (Text/Code).
- **Проверка перед запуском** (и перед «Продолжить»): все аккаунты проекта
  проверяются параллельно — браузер, вход, режим/модель, поле ввода. Если калибровка
  отсутствует или старше 72 ч, аккаунт калибруется автоматически. Проблемы видны
  сразу, до начала работы.
- **Перезапуск сервера** не роняет проекты: они продолжаются сами.
- Подробности: `RELIABILITY_PLAN.md`, найденные места для оптимизации: `CODE_REVIEW.md`.

### Тесты

```bash
cd server && .venv/bin/python -m pytest tests -q                 # ~6 мин
FRESH=1 E2E_SERVICE_URL="http://127.0.0.1:9100/?as=e2e&real=1&slow=3" \
  ADAPTER_MESSAGE_SELECTOR='[data-role="assistant"]' ADAPTER_INPUT_SELECTOR=textarea \
  ADAPTER_SEND_SELECTOR='button[aria-label="Send message"]' ADAPTER_LOGIN_SELECTOR='input[name=user]' \
  ADAPTER_SETUP_CLICKS= ../web/tests/e2e-server.sh
NODE_PATH=$(npm root -g) ADMIN_PASSWORD=adminpass123 node ../web/tests/e2e-stage11.mjs
```
