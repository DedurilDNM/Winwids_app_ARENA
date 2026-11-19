# Фаза 1 — Каркас (README_phase1)

Клиент-серверный каркас «AI Dev Department»: конфиг, БД, модели, JWT-аутентификация
с инвайтами, health-check, маскирование логов, автогенерация секретов. Кода Фаз 2–5
ещё нет — только структура директорий и рабочие заглушки/предварительно написанные
модули, которые Фазам 2–5 нужно будет наполнить.

## Что сделано

```
ai-dev-department/
├── DECISIONS.md                 # все решения по умолчанию
├── README.md                    # общий обзор + фазы
├── server/
│   ├── alembic.ini, alembic/    # миграции (env.py подключён к metadata моделей)
│   ├── requirements.txt
│   ├── .env.example
│   ├── README.md
│   ├── .env (не коммитится)
│   └── app/
│       ├── main.py              # FastAPI + lifespan, /health, /, маскирование, CORS, 429
│       ├── config.py            # pydantic-settings + автогенерация FERNET_KEY/JWT_SECRET
│       ├── db.py                # async engine, SessionLocal, init_db (create_all)
│       ├── models.py            # User, Invite, Account, Project, ProjectAssignment, Task, ConnectorCredential
│       ├── schemas.py           # pydantic v2 (EmailAddress — прагматичный, см. DECISIONS D-006)
│       ├── security.py          # JWT (access/refresh), bcrypt, Fernet
│       ├── deps.py              # get_db, get_current_user, get_current_admin, limiter
│       ├── admin.py             # CLI: create-invite / list-invites / list-users
│       ├── ws_manager.py        # менеджер WS-каналов project_id (API — Фаза 3)
│       ├── api/auth.py          # /api/auth/register|login|refresh|me (rate-limited)
│       ├── adapters/base.py     # Protocol ServiceAdapter + load_adapter()
│       └── adapters/generic/adapter.py  # стаб: is_alive() реальный, остальное NotImplementedError
├── client/                      # Фаза 4 (README-плейсхолдер)
└── deploy/                      # Фаза 5 (README-плейсхолдер)
```

## Как запустить

```bash
cd ai-dev-department/server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# (опционально) задать ADMIN_PASSWORD=... — иначе пароль админа выведется один раз в stdout
chmod 600 .env
uvicorn app.main:app --reload
```

Первый запуск сам: создаст FERNET_KEY/JWT_SECRET в `.env`, создаст схему БД,
засидит `admin@local` (пароль из ADMIN_PASSWORD или random, один раз в stdout).

## Проверка / acceptance criteria

| Критерий | Результат |
|---|---|
| `uvicorn app.main:app --reload` стартует | ✅ проверено на портах 8123/8124 (sandbox, Python 3.11) |
| `GET /health` → `{"status":"ok"}` | ✅ |
| `/docs` открывается (200) | ✅ |
| При первом запуске создаётся `admin@local` | ✅ (проверены оба пути: ADMIN_PASSWORD и random-пароль в stdout) |
| `POST /api/auth/login` возвращает JWT pair | ✅ (проверены `/login`, `/refresh`, `/api/auth/me`) |
| Регистрация только по инвайту из CLI | ✅ `python -m app.admin create-invite` → `/register` 201; повторный `/register` тем же кодом → 400 |
| Список пользователей | ✅ `python -m app.admin list-users` |
| Rate limit на /api/auth/* | ✅ 401×9 → затем `429` (лимит 10/мин) |
| Секреты в .env не коммитятся, chmod 600 | ✅ `.env`, `data/` в `.gitignore`; файл — 600; автогенерация без дублей ключей |
| Fernet roundtrip (storage_state) | ✅ encrypt→decrypt OK на сгенерированном FERNET_KEY |

Команды приёмки (копировать):

```bash
curl -s localhost:8000/health                       # {"status":"ok"}
curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@local","password":"<пароль>"}'  # -> access_token + refresh_token
python -m app.admin create-invite
```

Live preview (песочница): сервер запущен на `http://0.0.0.0:8123` — корневой
`GET /` отдаёт служебный JSON, `/docs` — Swagger UI.

## Осознанные отклонения / заметки

- Python в песочнице 3.11 — код написан совместимым (цель — 3.12+).
- `email-validator` не используется (см. DECISIONS.md, D-006) — пакет убран из requirements.
- Миграции: MVP = `create_all`; alembic готов к autogenerate (D-009).
- `ws_manager.py` реализован полностью уже сейчас, но WS-эндпоинт — Фаза 3.
- Адаптер generic: `send_message`/`wait_response` падают с `NotImplementedError` —
  это ожидаемо и допустимо для MVP (D-025).
