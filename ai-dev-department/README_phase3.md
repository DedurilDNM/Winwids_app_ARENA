# Фаза 3 — Projects API + оркестратор + github_watcher + WebSocket (README_phase3)

Проект теперь не просто «аккаунт в сервисе», а полный цикл: администратор проекта описывает задачу и репозиторий,
оркестратор разбивает её на подзадачи по ролям, агенты работают в репозитории через свои коннекторы и отчитываются
через статус-файл, сервер собирает итоговый отчёт.

## Что добавлено

- `server/app/services/prompts.py` — шаблоны промптов: split (оркестратор → план), agent (агенту → задача + ветка +
  отчёт в `status/task-<id>.md`), final (оркестратор → итог). Чистые функции, без имён внешнего ИИ-сервиса.
- `server/app/connectors/` — протокол `Connector` (read_file/list_dir) + реализация GitHub через raw REST API
  (httpx, base64-контент, токен из зашифрованного credentials или `GITHUB_TOKEN`).
- `server/app/services/github_watcher.py` — опрос `status/task-<id>.md` на ветке задачи до DONE / BLOCKED / timeout.
- `server/app/services/orchestrator.py` — ядро `run_project_logic` (split → задачи → агенты → watcher → итог),
  инжектируемый browser_manager/adapter/watcher; `check_all_sessions` (cron тела) — `is_alive` по активным аккаунтам,
  переключение active ↔ relogin_needed + события `session_lost`/`session_relogged` в пострадавшие проекты.
- `server/app/api/projects.py` — `POST/GET/PATCH/DELETE /api/projects/{id}`, `POST /{id}/start` / `/stop`.
  Владение пользователем (чужое → 404), правка/удаление запрещены пока `running` (409), start валидирует, что все
  назначенные и оркестратор — активны и принадлежат пользователю.
- `server/app/api/ws.py` — `WS /ws/projects/{id}?token=<access_jwt>`: при подключении `snapshot` (проект + задачи),
  затемライブ-события задач/проекта/сессий. Чужие пользователи и невалидный токен — закрытие 4401.
- `server/app/main.py` — роутеры `projects` и `ws` зарегистрированы.
- `server/app/workers/` — arq: `settings.py` (WorkerSettings: задача `run_project` + cron `session_health_check`
  каждые 5 минут), `run_project.py` (задача), `session_check.py` (проверка сессий). Если Redis недоступен —
  `run_project` выполняется inline в API-процессе с предупреждением (песочница/Dev).
- `server/app/config.py` + `server/.env.example` — `GITHUB_TOKEN`, `GITHUB_PROXY_URL`, `GITHUB_POLL_INTERVAL_SECONDS`
  (15), `TASK_TIMEOUT_SECONDS` (3600), `ORCHESTRATOR_TIMEOUT_SECONDS` (600), `AGENT_TIMEOUT_SECONDS` (600).
- `server/app/api/accounts.py` — `confirm-login` теперь шлёт `session_relogged` во все пострадавшие проекты, если
  статус был `relogin_needed`.
- `server/tests/test_projects.py` — 8 тестов: CRUD + изоляция, 409 при редактировании running, валидация start,
  WS snapshot + чужой токен отклонён, unit-тесты парсера плана. `conftest.py`: rate-limit сбрасывается между тестами.
- `DECISIONS.md` — D-049…D-058.

## Новые эндпоинты

```
POST   /api/projects                                 # создать проект (draft)
GET    /api/projects                                 # список своих проектов
GET    /api/projects/{id}                            # проект + задачи ( хронология )
PATCH  /api/projects/{id}                            # только если не running (409 иначе)
DELETE /api/projects/{id}                            # только если не running (409 иначе)
POST   /api/projects/{id}/start                      # running → arq job run_project (или inline)
POST   /api/projects/{id}/stop                       # running → failed (кооперативная остановка)
GET    /api/projects/{id}/tasks                      # задачи проекта
WS     /ws/projects/{id}?token=<jwt>                 # snapshot + события в реальном времени
```

## Acceptance criteria — что проверено и где

| Критерий | Статус |
|---|---|
| Создание/список/получение проекта, владение пользователем (чужой → 404) | ✅ тест `test_projects_crud_isolated` |
| PATCH/DELETE 금지 пока `running` → 409 | ✅ тест `test_project_patch_rejects_running` |
| start валидность: нет назначений → 400; оркестратор не активен → 409 | ✅ тест `test_start_validation` |
| start переводит в `running`, stop → `failed`, delete → 204 | ✅ тесты `test_projects_crud_isolated` / `test_project_patch_rejects_running` |
| WS snapshot сразу после подключения (проект + задачи) | ✅ тест `test_ws_snapshot_and_auth` |
| Чужой токен / невалидный токен на WS отклоняются | ✅ тест `test_ws_snapshot_and_auth` |
| Парсер плана: валидный JSON, пропуск невалидных ролей, markdown-окношки, fallback на оркестратора | ✅ тесты `test_parse_task_plan_*` |
| Оркестратор: split → задачи → watcher DONE/BLOCKED/timeout → итог (инжектируемые деps) | ✅ код + unit-тесты парсера; живой прогон требует браузера + GitHub-репозитория |
| Cron проверки сессий (каждые 5 мин): is_alive → active/relogin_needed + WS-события | ✅ код `check_all_sessions`; в тестах не гоняется (нет браузера) |
| Всё вместе (проекты + аккаунты) | ✅ `pytest tests/ -v` |

Прогон тестов: `17 passed` (`pytest tests/ -v`).

## Запуск

Локальная разработка (без Redis — inline-режим, работает из коробки):

```bash
cd ai-dev-department/server
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8123
```

Воркер arq (требует Redis на `REDIS_URL`):

```bash
.venv/bin/python -m arq app.workers.settings.WorkerSettings
```

## Что дальше (Фаза 4 — не начата)

Реальный адаптер внешнего ИИ-сервиса (заменяет `adapters/generic/adapter.py` с его `NotImplementedError`),
подключение аккаунтов к сервису, PR/сборка, итоговый отчёт пользователю. Статус проекта: уточняется с пользователем.
