# DECISIONS — решения по умолчанию (приняты без вопросов, по правилу №1)

Все решения ниже приняты как разумные дефолты. Канонические требования — в задаче
«AI Dev Department». Если пункт противоречит задаче, задача побеждает; пиши issue/правку.

## Общие

- **D-001.** Код лежит в подкаталоге `ai-dev-department/` внутри репозитория `Winwids_app_ARENA`
  (спека задаёт дерево с корнем `ai-dev-department/`). README репозитория указывает на него.
- **D-002.** Язык документации и комментариев — английский в коде, русский в ответах/README
  пользователя (как просили). `README_phaseN.md` — по-русски.
- **D-003.** Окружение разработки — Linux sandbox (Debian-based), Python 3.11 доступен в песочнице,
  целевая платформа сервера — Debian 13 с Python 3.12+. Код не использует синтаксис 3.12+,
  поэтому проверяется и на 3.11; в проде 3.12/3.13.
- **D-004.** Датасеты/секреты не коммитятся: `.env`, `server/data/`, `.venv/` — в `.gitignore`.

## Сервер

- **D-005. Именование адаптера в .env.** `ADAPTER_MODULE=app.adapters.generic.adapter`
  (полное путевое имя пакета `app`). Загрузчик (`adapters/base.load_adapter`) дополнительно
  принимает «голую» форму `adapters.generic.adapter` из примера спеки, подставляя префикс `app.`.
- **D-006. Email-валидация.** Вместо `email-validator` (EmailStr) используется собственный
  прагматичный валидатор `EmailAddress` в `schemas.py`: один `@`, непустые части, ≤254 символов.
  Причина: EmailStr отвергает односоставные домены вида `admin@local`, а seed-админ —
  именно `admin@local` (требование спеки).
- **D-007. Пароли.** bcrypt через passlib, пин `bcrypt==4.0.1` (passlib 1.7.4 несовместим с bcrypt ≥ 4.1).
- **D-008. JWT.** Access 15 мин (`sub`=user id, `type=access`), refresh 30 дней (`type=refresh`).
  Refresh stateless, ротация при каждом `/refresh` (выдаётся новая пара). Хранение refresh на клиенте —
  в Windows Credential Manager через `keyring` (Фаза 4). Отзыв refresh — не реализован в MVP (зафиксировано).
- **D-009. Создание схемы БД.** На старте выполняется `create_all` (MVP). Alembic полностью настроен
  (`alembic.ini`, `alembic/env.py` с метаданными моделей и URL из конфига) для последующих миграций;
  autogenerate — `alembic revision --autogenerate`.
- **D-010. Наивное UTC в БД.** Все `DateTime` — naive UTC (`utcnow()` в models.py), чтобы поведение
  SQLite и PostgreSQL совпадало без конверсий. JWT `exp/iat` — timezone-aware (обрабатывает jose).
- **D-011. Назначение аккаунтов на проект.** Отдельная таблица `project_assignments`
  (project_id, account_id, role, unique пары) вместо JSON-колонки: целостность на уровне БД,
  готовность к PostgreSQL. Сверх списка моделей из спеки — осознанное расширение.
- **D-012. ConnectorCredential.** Заложена таблица для Fernet-зашифрованных токенов коннекторов
  (GitHub-токен пользователя для server-side watcher'а). Сам GitHub-коннектор сервиса мы не реализуем.
- **D-013. Сидирование админа.** Первый запуск при пустой таблице users создаёт `admin@local`:
  пароль из `ADMIN_PASSWORD`, иначе random `secrets.token_urlsafe(12)`, выводится в stdout **один раз**
  (с `flush=True`, иначе при SIGTERM баннер терялся в буфере).
- **D-014. FERNET_KEY / JWT_SECRET.** Автогенерация на первом запуске `get_settings()` (вызывается
  до создания Settings): пустые плейсхолдеры в `.env` заменяются на месте, дубликаты ключей в файле
  не возникают, файл получает `chmod 600`. Значения из реального окружения всегда выигрывают.
- **D-015. DATABASE_URL пустой в .env = дефолт.** Валидаторы в config.py заменяют пустые значения
  `DATABASE_URL`/`GITHUB_API_URL` на дефолты (иначе пйdantic подставил бы `""`).
- **D-016. Rate limit.** slowapi, key = IP (`get_remote_address`). `/api/auth/register` и `/login` — 10/мин,
  `/refresh` — 30/мин, `/me` — 60/мин. При превышении — HTTP 429.
- **D-017. CORS.** Дефолт — только localhost (5173/3000). Прод: `CORS_ORIGINS=https://app.<domain>` в .env.
  Нативный клиент Origin не шлёт — CORS для него несущественен; это для dev-веб/отладки.
- **D-018. Логирование.** Запросы логируются как `METHOD path?masked_query`; тела запросов не логируются
  вовсе; ключи `password/token/secret/cookie/authorization/invite_code/…` маскируются (`***`).
  Необработанные исключения → 500 `{"detail":"Internal server error"}` без утечки internals.
- **D-019. Health/Service.** `GET /health` → `{"status":"ok"}` (контракт спеки). `GET /` → сервисная
  информация без секретов. `/docs` и `/openapi.json` включены (MVP; на проде при желании отключить).
- **D-020. Маскирование в URL.** noVNC-ссылки (Фаза 2) содержат короткоживущий account-JWT в query —
  такие параметры попадут под маскирование (`token` ∈ SENSITIVE_KEYS).
- **D-021. Инвайты.** Только CLI (`python -m app.admin create-invite [--expires-hours N]`), одноразовые,
  хранят code/used/used_by/used_at/expires_at/created_by. Админские HTTP-эндпоинты инвайтов — не в MVP.
- **D-022. Коды ошибок регистрации.** Невалидный/использованный/просроченный инвайт → 400 (без деталей,
  кроме «invalid or used»/«expired»); занятый email → 409; неверные креды → 401 (uniform, без раскрытия
  существования email).
- **D-023. Расположение AGENTS.md/спека.** Сама задача не коммитится как есть; её решения зафиксированы
  здесь и в README.
- **D-024. `ws_manager.py`.** Реализован уже в Фазе 1 (не заглушка): менеджер каналов project_id → sockets,
  `send_to_project`, `broadcast`. API `/ws/projects/<id>` — Фаза 3.
- **D-025. Адаптер generic.** `is_alive` — реальная проверка открытия `SERVICE_URL` (status < 500),
  остальные методы — `NotImplementedError`; `get_state` → `"idle"` (контракт MVP). Оркестратор поэтому
  не сможет реально «поговорить» с агентами без подмены адаптера — ожидаемо и задокументировано.
- **D-026. Прокси.** `PROXY_URL` читается конфигом и будет применяться в Camoufox/httpx (Фаза 2+).
  Никакой прямой маршрутизации мимо прокси не добавляется.
- **D-027. Тестовый стенд.** `SERVICE_URL=https://example.com`, `PROXY_URL=http://user:pass@1.2.3.4:8080`
  — как в спеке; адаптер откроет example.com и вернёт idle.
- **D-028. Порты песочницы.** Временные тестовые серверы поднимались на 8123/8124 — на продукт не влияют.
- **D-029. Роли.** Каталог ролей — строка + Literal в pydantic (`Architect|Coder|Reviewer|Tester|Integrator|Orchestrator`),
  без отдельной таблицы; БД хранит string.
- **D-030. .env.example** содержит пустые `FERNET_KEY=`/`JWT_SECRET=` (маркеры «автогенерация») и пустой
  `DATABASE_URL=` (трактуется как дефолт, см. D-015). Реальный `.env` в git не попадает.
