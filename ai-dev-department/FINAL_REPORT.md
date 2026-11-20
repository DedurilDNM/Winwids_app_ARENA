# Итоговый отчёт по проекту Winwids_app_ARENA

Ветка: `arena/5c0db555-winwids-app-arena` (ответвлена от `main` c15aabd2)
Последний коммит: `d67162e` (Фаза 5)
Статус рабочего дерева: чисто
Превью-сервер: `http://0.0.0.0:8123`, `GET /health` → `{"status":"ok"}`
Все тесты: **31 passed** (`pytest tests/ -q`)

---

## Краткое содержание

Реализованы все 5 фаз проекта «AI Dev Department» — система управления командой
ИИ-агентов над общим проектом. Сервер на FastAPI (Python 3.11), десктопный
клиент на PySide6 (Windows), деплой на Debian через `deploy/install.sh`.
Внешний ИИ-сервис не зашит в код — всё управление через `SERVICE_URL` +
настраиваемые селекторы адаптера.

---

## Фаза 1 — Каркас, JWT-auth, инвайты (коммит af76661)

- `app/config.py` — настройки, автогенерация `FERNET_KEY`/`JWT_SECRET` в `.env`
  (chmod 600), Fernet-шифрование, JWT HS256.
- `app/models.py` — `User`, `Invite`, `Account`, `Project`, `ProjectAssignment`,
  `Task`, `ConnectorCredential`.
- `app/security.py` — bcrypt пароли, JWT access/refresh, encrypt/decrypt.
- `app/db.py` — SQLAlchemy async, `init_db()`, seed админа `admin@local`.
- `app/api/auth.py` — регистрация по инвайту, логин, refresh, `/me`, rate limit.
- `app/main.py` — lifespan, CORS, маскировка敏感 query-параметров, `/health`.
- `app/admin.py` — CLI для администрирования.
- `adapters/` — протокол `ServiceAdapter` + stub (заглушка).
- Тесты: `tests/test_accounts_flow.py` (9 тестов, фейковый Camoufox).

---

## Фаза 2 — VNC-логин, аккаунты (коммит 86170b4)

- `app/services/browser_manager.py` — менеджер Camoufox-контекстов (singleton),
  запуск/остановка, экспорт `storage_state` (cookies + localStorage + IndexedDB),
  восстановление активных сессий после рестарта.
- `app/services/vnc_manager.py` — Xvfb (:97+, shared :99) + x11vnc (localhost)
  + websockify (:6080) с TokenFile-маршрутизацией по кратковременному JWT.
- `app/api/accounts.py` — CRUD аккаунтов (лимит 10/пользователь),
  `confirm-login`, изоляция по владельцу, DELETE чистит ссылки.
- `app/api/vnc.py` — `POST/GET/STOP /api/accounts/{id}/vnc`.
- `app/main.py` — lifespan: restore активных аккаунтов + graceful shutdown.
- `deploy/install.sh` — bootstrap Debian (xvfb, x11vnc, novnc, websockify,
  redis, venv, `camoufox fetch`, `.env` chmod 600).
- Тесты: 9 passed.

---

## Фаза 3 — Projects API + оркестратор + github_watcher + WebSocket (коммит 08150ba)

- `app/api/projects.py` — `POST/GET/PATCH/DELETE /api/projects/{id}`,
  `POST /{id}/start` / `/stop`, `GET /{id}/tasks`.
  Владелец-only (чужое → 404), правка/удаление запрещены пока `running` (409),
  `start` валидирует активность оркестратора и назначенных.
- `app/api/ws.py` — `WS /ws/projects/{id}?token=<jwt>`: `snapshot` при
  подключении + события `task_started`, `task_status_changed`,
  `project_started/completed/failed`, `session_lost`, `session_relogged`.
- `app/services/orchestrator.py` — `run_project_logic`: split-план → задачи →
  агенты → watcher DONE/BLOCKED/timeout → итог; `check_all_sessions` (cron тела).
- `app/services/github_watcher.py` — опрос `status/task-<id>.md` на ветке задачи
  через `Connector` (GitHub raw REST, httpx).
- `app/services/prompts.py` — шаблоны split / agent / final.
- `app/connectors/` — протокол `Connector` + GitHub через raw REST.
- `app/workers/` — arq `WorkerSettings`: задача `run_project` + cron
  `session_health_check` каждые 5 мин; inline-режим без Redis.
- Тесты: `tests/test_projects.py` (8 тестов) + 기존 9 = 17 passed.

---

## Фаза 4 — Реальный адаптер ИИ-сервиса + подключение аккаунтов (коммит 03729be)

- `app/adapters/generic/adapter.py` — рабочий селекторный адаптер протокола
  `ServiceAdapter`: `open_chat`, `send_message`, `wait_response`, `get_state`,
  `is_alive`. Селекторы через `ADAPTER_*` в конфиге, сервис не зашит.
- `app/config.py` — 10 новых настроек адаптера (селекторы, таймауты, повторения,
  бэкофф).
- `app/services/orchestrator.py` — `_ask` повторяет `open_chat`+`send_message`
  с экспоненциальным бэкоффом на временные ошибки; `wait_response` не
  повторяется (без дублирования запроса).
- Подключение аккаунта: ручной VNC-логин → `confirm-login` сохраняет
  Fernet-шифрованный `storage_state` → оркестратор запускает браузер с этим
  состоянием → сервис видит сессию аккаунта.
- Тесты: `tests/test_adapter.py` (15 тестов, фейковая DOM-страница) = 31 passed
  всего.

---

## Фаза 5 — PySide6-клиент (Windows, вариант «Ядро») (коммит d67162e)

- `client/app/main.py` — точка входа PySide6, `API_BASE_URL` из env.
- `client/app/api_client.py` — httpx клиент: Bearer-токен, авто-refresh при 401.
- `client/app/auth_store.py` — токены только в памяти.
- `client/app/windows/login.py` — вход / регистрация с инвайт-кодом.
- `client/app/windows/main.py` — список проектов: поиск, создание, старт/стоп/удаление.
- `client/app/windows/project_detail.py` — деталь проекта: статус, результат,
  таймлайн задач, старт/стоп/обновить.
- `client/build.spec` — PyInstaller standalone `.exe` для Windows.
- `client/requirements-client.txt` — `PySide6>=6.5.0`, `httpx>=0.27.0`.
- Все файлы клиента проходят `py_compile`.

---

## Документация и решения

- `DECISIONS.md` — 67 решений (D-001 … D-067).
- `README_phase1.md` … `README_phase5.md` — по одной странице на фазу.
- Корневой `README.md` — статус всех фаз.

---

## Архитектура (кратко)

```
Клиент (PySide6, Windows)  HTTPS  Сервер (FastAPI, :8123)
                                    |
                                    v
                          +---------------------+
                          |  API (auth/projects/|---> SQLite (users, accounts,
                          |  accounts/vnc/ws)   |   projects, tasks, …)
                          +---------------------+
                                    |
                                    v
                          +---------------------+
                          |  BrowserManager     |---> Camoufox (headless/VNC)
                          |  (singleton)        |     storage_state -> зашифровано
                          +---------------------+     в Account
                                    |
                                    v
                          +---------------------+
                          |  Orchestrator (arq) |---> GitHub watcher (status/*.md)
                          +---------------------+     -> внешний ИИ-сервис через адаптер
```

---

## Как запустить

### Сервер (разработка)
```bash
cd ai-dev-department/server
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8123
curl http://127.0.0.1:8123/health   # {"status":"ok"}
```

### Воркер arq (требует Redis)
```bash
.venv/bin/python -m arq app.workers.settings.WorkerSettings
```

### Тесты
```bash
cd ai-dev-department/server
.venv/bin/python -m pytest tests/ -v   # 31 passed
```

### Клиент (Windows, или Linux с X-сервером)
```bash
cd ai-dev-deployment/client
../server/.venv/bin/pip install -r requirements-client.txt
API_BASE_URL=http://127.0.0.1:8123 ../server/.venv/bin/python -m app.main
```

### Сборка .exe (Windows)
```bash
cd ai-dev-department/client
pyinstaller build.spec
```

### Деплой на Debian
```bash
sudo bash deploy/install.sh   # ставит стек, venv, camoufox fetch, .env
cd server && ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Ограничения / что можно доработать

1. В клиенте пока нет окон «Аккаунты» и «VNC-просмотр» (вариант «Ядро»).
   Можно добавить по аналогии с существующими окнами.
2. `app/api/auth/refresh` эндпоинт на сервере не реализован — авто-refresh в
   клиенте пока заглушка. При добавлении эндпоинта — реализовать в
   `auth_store.refresh()`.
3. Полноценный запуск GUI клиента требует Windows или Linux с X-сервером +
   libGL (в песочнице без X — только `py_compile`).
4. systemd-юниты и cloudflared для продакшена — `deploy/install.sh` печатает
   hints, сами юниты не созданы.

---

## Статус

Все 5 фаз завершены, код закоммичен и запушен на
`arena/5c0db555-winwids-app-arena`. Проект готов к использованию в режиме
разработки и к доработке под продакшен (systemd, cloudflared, окна аккаунтов
в клиенте).
