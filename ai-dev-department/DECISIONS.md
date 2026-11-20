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

## Фаза 2 (accounts + VNC)

- **D-031. VNC-стек.** Per-account Xvfb :97.. (блокировка номеров через `display-N.lock` и
  проверку `/tmp/.X11-unix`), x11vnc на localhost rfb-портах 5901+, ОДИН глобальный websockify на
  6080 с `--token-plugin=websockify.token_plugins.TokenFile --token-source=data/vnc/tokens.cfg`.
  tokens.cfg переписывается на каждое изменение сессии (строки `<token>: 127.0.0.1:<rfb_port>`), chmod 600.
- **D-032. Токен noVNC.** JWT `type=vnc`, `sub=account_id`, TTL 120 минут (`_create_token` в security.py).
  websockify проверяет наличие токена в tokens.cfg; криптографическая валидация JWT — не делается
  (токен непредсказуем, 32+ символа). Expiry — по TTL, удаление — при stop_session/confirm-login.
  noVNC отдаётся как `{NOVNC_PUBLIC_BASE_URL}/vnc.html?autoconnect=true&resize=scale&token=<jwt>`
  (спецификация требовала `wss://.../novnc?token=` — это схематично; реальный URL указывает на
  статическую страницу noVNC, см. также D-036 про path mapping в проде).
- **D-033. noVNC URL path в проде.** `NOVNC_PUBLIC_BASE_URL=https://app.<domain>/novnc`; cloudflared/nginx
  маппит `/novnc/*` → `127.0.0.1:6080/*` (страницы) и `/websockify` (websocket). Локальные дефолты:
  `http://127.0.0.1:6080` (прямой доступ к websockify).
- **D-034. Restore на старте.** Lifespan: `ensure_shared_display()` (Xvfb :99) →
  `browser_manager.restore_active_accounts(...)` → активные аккаунты поднимаются из Fernet-зашифрованного
  storage_state. Ошибки одной учётки не роняют старт (лог + warning). Без X-сервера / в VNC_HEADLESS —
  деградация в headless с предупреждением.
- **D-035. Хранение storage_state.** Cookie + localStorage + IndexedDB (попытка `storage_state(indexed_db=True)`,
  fallback на обычный вызов — Playwright ≥1.49 experimental). Сериализация JSON (bytes), Fernet-шифрование,
  запись в `accounts.storage_state_encrypted`. Пароль сервиса в БД/логи/API не попадает никогда.
- **D-036. Капча/ручной логин.** Управлять Camoufox-окном пользователь НЕ может, кроме как через VNC —
  это требование безопасности (никаких обходов капчи сервером).
- **D-037. confirm-login.** Требует живой контекст (иначе 409). После сохранения storage_state статус →
  `active`; закрывается только noVNC-путь (x11vnc + удаление токена), браузер остаётся жить (спека).
- **D-038. Лимит 10 аккаунтов на пользователя** — на `POST /api/accounts` (403 + сообщение). PUT/PATCH нет.
- **D-039. PATCH /api/accounts/{id}.** Разрешены name/service_note/default_role/github_repos/status;
  принудительно запретить `status=active` (только через confirm-login). `disabled` — через PATCH разрешён.
- **D-040. DELETE.** Останавливает VNC-сессию И браузер, затем NULL-ит `tasks.assigned_account_id` и
  `projects.orchestrator_account_id`, удаляет `project_assignments`, удаляет аккаунт. 204.
- **D-041. VNC при недоступности стека.** POST /api/accounts НЕ падает: 201 + поле `vnc_error` с причиной
  (account создан, pending_login; повтор — `POST /{id}/vnc/start`, при ошибке 503).
- **D-042. VNC_HEADLESS=true** — тестовый режим (CI/песочница): Camoufox поднимается без Xvfb/x11vnc/noVNC,
  `vnc_url` отсутствует, confirm-login работает полностью. По умолчанию false.
- **D-043. Delete не трогает Xvfb shared display :99** (его может использовать restore); per-account Xvfb
  останавливается только при keep_browser=False.
- **D-044. Websockify профиль токена.** `--token-plugin=websockify.token_plugins.TokenFile` (полное имя
  класса — сокращённое `TokenFile` зависит от версии). Токены в файле перечитываются websockify'ем на
  каждое WS-соединение. Проверено в песочнице: vnc.html=200, валидный токен → 101 + backend connected,
  невалидный → соединение разорвано/пусто.
- **D-045. noVNC в песочнице.** deb.debian.org недоступен → системные Xvfb/x11vnc не ставятся;
  проверка токен-маршрутизации выполнена на websockify (pip) + noVNC (git clone GitHub) + dummy RFB
  backend. Графическая часть (Camoufox в окне + ручной логин) проверяется на целевом Debian
  через deploy/install.sh. Acceptance п. «вижу Camoufox в noVNC» — за пользователем на Debian.
- **D-046. Тесты.** `tests/test_accounts_flow.py` — 9 тестов с фейковым Camoufox (in-memory context,
  storage_state из JSON): шифрование storage_state в БД, restore после «рестарта», 409/503, изоляция
  пользователей, лимит 10, CRUD. Запуск: `pip install -r requirements-dev.txt && pytest tests/ -v`.
- **D-047. .env пример** дополнен NOVNC_* / WEBSOCKIFY_* / XVFB/VNC_* (см. server/.env.example).
- **D-048. x11vnc слушает только localhost** (`-localhost`), доступ снаружи — только через websockify+token.
  Xvfb без `-nolisten tcp` не запускается.

- **D-049. Projects API.** `POST/GET/PATCH/DELETE /api/projects/{id}` + `POST /{id}/start` / `/stop`.
  Владение пользователем: чужие проекты невидимы (404). Редактирование/удаление запрещены, пока проект
  `running` (409). Схема: `ProjectCreate/Out/Update/DetailOut`, `TaskOut`, `ProjectAssignmentIn`.
- **D-050. WebSocket.** `WS /ws/projects/{id}?token=<access_jwt>`. При подключении отсылается `snapshot`
  (проект + задачи), затем события `task_started`, `task_status_changed`, `project_started`,
  `project_completed`, `project_failed`, `session_lost`, `session_relogged`. Чужие пользователи и невалидный
  токен — закрытие 4401.
- **D-051. Оркестратор (`services/orchestrator.py`).** Поток: промпт оркестратора → JSON-план
  [{role,task,assign_to_account_id}] → Task-rows → каждая задача последовательно: агент работает в репозитории
  через свой GitHub-коннектор → github_watcher опрашивает `status/task-<id>.md` (DONE/BLOCKED/timeout) → итоговый
  промпт оркестратора → `project_completed`. Парсер терпит markdown-окношки, валидирует роли, fallback на
  оркестратора/первое назначение. browser_manager/adapter/watcher инжектируемы (тесты без браузера/Redis).
- **D-052. github_watcher.** Polling `status/task-<id>.md` на ветке задачи через `Connector` (GitHub raw REST
  через httpx, токен из зашифрованного `ConnectorCredential` пользователя или `GITHUB_TOKEN`). Первая непустая
  строка файла: DONE→done, BLOCKED→blocked, иначе None (ещё не завершено).
- **D-053. Connectors.** `app/connectors/base.py` протокол `Connector` (read_file/list_dir); `connectors/github.py`
  — реализация через GitHub REST API (base64-контент). Не именует внешний ИИ-сервис.
- **D-054. Воркеры arq.** `workers/settings.py` (WorkerSettings: `run_project` + cron `session_health_check`
  каждые 5 мин), `workers/run_project.py` (задача `run_project`), `workers/session_check.py` (проверка сессий).
  Если Redis недоступен — `run_project` выполняется inline в API-процессе с логом предупреждения (песочница/Dev).
  `job_timeout=6h`, `max_tries=1` (оркестратор сам ставит failed), не повторяем тихо.
- **D-055. Проверка сессий (cron).** Каждые 5 мин `check_all_sessions`: `adapter.is_alive` для активных аккаунтов.
  active→mёртв: `relogin_needed` + `session_lost` в пострадавшие проекты; `relogin_needed`→жив: `active` +
  `session_relogged`. WS события рассылаются менеджером соединений.
- **D-056. Перелогин аккаунта.** POST /api/accounts/{id}/confirm-login: при смене статуса с `relogin_needed` на
  `active` шлёт `session_relogged` во все пострадавшие проекты (аккаунт снова пригоден для оркестрации).
- **D-057. Config Phase 3.** Добавлено: `GITHUB_TOKEN`, `GITHUB_PROXY_URL`, `GITHUB_POLL_INTERVAL_SECONDS`
  (15), `TASK_TIMEOUT_SECONDS` (3600), `ORCHESTRATOR_TIMEOUT_SECONDS` (600), `AGENT_TIMEOUT_SECONDS` (600).
- **D-058. Tests Phase 3.** `tests/test_projects.py`: CRUD + изоляция, 409 при редактировании running,
  валидация start (нет назначений/оркестратора не активен → 400/409), WS snapshot + чужой токен отклонён,
  unit-тесты парсера плана. Rate-limit сбрасывается между тестами (`conftest.py`, autouse). Всего 17 тестов.

- **D-059. Реальный адаптер внешнего ИИ-сервиса (`app/adapters/generic/adapter.py`).**
  Заменён stub (NotImplementedError) на рабочий селекторный адаптер протокола
  `ServiceAdapter`: `open_chat`, `send_message`, `wait_response`, `get_state`,
  `is_alive`. Все селекторы ввода/отправки/сообщений/статуса/логина настраиваются
  через настройки (`ADAPTER_*`), конкретный сервис не зашит — только `SERVICE_URL`.
  Адаптер принимает опциональный `settings`-объект (для тестов) или `get_settings()`.
- **D-060. Подключение аккаунта к сервису.** Аккаунт логинится вручную через VNC
  (Фаза 2), `storage_state` (cookies + localStorage + IndexedDB) сохраняется
  Fernet-шифрованным на `Account.storage_state_encrypted`. `BrowserManager.launch`
  перезапускает Camoufox с этим состоянием, поэтому адаптер работает в контексте
  именно этого аккаунта — сервис видит его сессию, сервер пароля не знает.
- **D-061. Устойчивость оркестратора.** `_ask` повторяет `open_chat` +
  `send_message` с экспоненциальным бэкоффом (`ADAPTER_MAX_RETRIES`,
  `ADAPTER_RETRY_BACKOFF_SECONDS`) на временные ошибки браузера/сети.
  `wait_response` не повторяется (избежать дублирования запроса), но падает
  быстро на `error` / `login_required` / `TimeoutError`.
- **D-062. Состояния адаптера.** `get_state` возвращает `idle` / `generating` /
  `error` / `login_required` по атрибутам состояния DOM (`data-state`, `aria-busy`
  и т.п.) и селектору логина-формы. `wait_response` ориентируется на `get_state`
  и возвращает текст последнего нового сообщения после перехода в `idle`.
- **D-063. Config Phase 4.** Добавлено: `ADAPTER_INPUT_SELECTOR`,
  `ADAPTER_SEND_SELECTOR`, `ADAPTER_MESSAGE_SELECTOR`, `ADAPTER_STATE_SELECTOR`,
  `ADAPTER_LOGIN_SELECTOR`, `ADAPTER_READY_SELECTOR`,
  `ADAPTER_POLL_INTERVAL_SECONDS` (2.0), `ADAPTER_NAVIGATE_TIMEOUT_SECONDS` (30),
  `ADAPTER_MAX_RETRIES` (3), `ADAPTER_RETRY_BACKOFF_SECONDS` (2.0).
- **D-064. Tests Phase 4.** `tests/test_adapter.py` — 15 тестов адаптера с
  изолированными настройками и фейковой DOM-страницей (без Camoufox и без
  реального сервиса): навигация в чат, заполнение/отправка, fallback на Enter,
  ошибка логина, возврат ответа ассистента, состояния error/login_required/idle/
  generating, таймаут, `is_alive` при обрыве сети. Всего 31 тест.

- **D-065. PySide6-клиент (Windows, Фаза 5, вариант «Ядро»).**
  Окна: вход/регистрация (LoginWindow), список проектов (MainWindow) с поиском,
  созданием, запуском/остановкой/удалением, деталь проекта с таймлайном задач
  (ProjectDetailWindow). API-клиент (api_client.py) на httpx с Bearer-токеном и
  авто-refresh при 401. Токены хранятся только в памяти (AuthStore), не в
  QSettings. Точка входа: app/main.py, base_url из API_BASE_URL (по умолчанию
  :8123 сервера).
- **D-066. PyInstaller-сборка.** build.spec для standalone .exe на Windows.
  Собирать на целевой машине с PySide6; hiddenimports включают PySide6
  подмодули и все модули клиента.
- **D-067. Клиент не зависит от libGL в CI.** В песочнице без X-сервера/меса
  проверка ограничена py_compile (все файлы компилируются). Полноценный запуск
  GUI требует Windows с PySide6.

## Web client instead of PySide6 (stage 1)
The app must run on PC and phone. One web client (PWA, vanilla JS, no build
step) is served by the API at `/app/`; it installs on phones and desktops.
The PySide6 client is kept in `client/` but no longer developed.

## Agents do not use GitHub; the server commits (stage 3)
The external service requires selecting the repository in every new chat,
and the orchestrator opens a new chat per task, so relying on the service's
GitHub connector would mean automating that UI step for every task. Instead:
- the server appends an OUTPUT CONTRACT to every agent prompt (`FILE: path`
  + fenced full content, then `STATUS: DONE|BLOCKED` + `SUMMARY:`), so the
  orchestrator never has to restate the format;
- the reply is parsed (`services/agent_output.py`, tolerant to rendered
  markdown, path traversal rejected); one "resend in format" retry;
- the server commits the files + `status/task-<id>.md` to `ai/project-<id>`
  with the user's fine-grained token (Contents + Pull requests: read/write,
  stored Fernet-encrypted), and opens a PR to the default branch at the end;
- each next agent gets the repo file list and the contents of files written
  by earlier tasks (60 KB budget), so agents build on each other's work.
Agents cannot run code in this mode; using the service connector can be
added later as an option.

## Schema changes without Alembic (stage 3)
New columns are nullable and added on startup by `db._add_missing_nullable_columns`
(`ALTER TABLE ... ADD COLUMN`), so existing SQLite databases keep working.
