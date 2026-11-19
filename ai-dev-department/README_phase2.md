# Фаза 2 — API аккаунтов + VNC-логин (README_phase2)

Добавлен полный VNC-flow: создание аккаунта → Xvfb → Camoufox → x11vnc → noVNC/websockify →
ручной логин пользователем → confirm-login → Fernet-зашифрованный storage_state в БД →
восстановление активных сессий после рестарта сервера.

## Что добавлено

- `server/app/services/browser_manager.py` — менеджер Camoufox-контекстов (singleton),
  экспорт storage_state (cookies + localStorage + IndexedDB), restore после рестарта.
  Весь трафик к сервису — через `PROXY_URL` (browser-level proxy).
- `server/app/services/vnc_manager.py` — Xvfb (:97+, shared :99) + x11vnc (localhost, rfb 5901+) +
  один глобальный websockify (:6080) с TokenFile-маршрутизацией по короткоживущему JWT-токену.
- `server/app/api/accounts.py` — CRUD аккаунтов (лимит 10/пользователь), `confirm-login`,
  строгая изоляция по владельцу, DELETE чистит ссылки (tasks/projects/assignments).
- `server/app/api/vnc.py` — `POST /vnc/start`, `POST /vnc/stop`, `GET /vnc`.
- `server/app/main.py` — lifespan: restore активных аккаунтов + graceful shutdown
  (браузеры, Xvfb, websockify).
- `deploy/install.sh` — установка на чистый Debian (apt: xvfb, x11vnc, novnc, websockify,
  redis, python-venv …; pip deps; `camoufox fetch`; .env с chmod 600). Идемпотентен.
- `server/tests/` — 9 тестов с фейковым Camoufox (`pytest tests/ -v`).
- `requirements.txt` + `websockify`; `requirements-dev.txt` — pytest.

## API (сверх Фазы 1)

```
POST   /api/accounts                      # создать pending_login + сразу открыть VNC-сессию
GET    /api/accounts                      # список своих аккаунтов
GET    /api/accounts/{id}
PATCH  /api/accounts/{id}                 # name/service_note/default_role/github_repos/status(disabled)
DELETE /api/accounts/{id}                 # стоп VNC+браузер, 204
POST   /api/accounts/{id}/vnc/start       # (повторно) поднять VNC-сессию -> vnc_url
POST   /api/accounts/{id}/vnc/stop        # закрыть noVNC-путь, браузер живёт
GET    /api/accounts/{id}/vnc             # состояние сессии
POST   /api/accounts/{id}/confirm-login   # «Готово» -> storage_state зашифрован, статус active
```

Ответ создания при недоступном VNC-стеке (например, нет Xvfb): `201` + `vnc_error` с причиной;
аккаунт остаётся `pending_login`, повтор — `POST /{id}/vnc/start` (→ 503 при ошибке).

## Acceptance criteria — что проверено и где

| Критерий | Статус |
|---|---|
| Создание аккаунта через API → `pending_login` | ✅ тестами и curl (см. ниже) |
| VNC-flow: сессия поднимается, клиент получает noVNC URL | ✅ код + проверка TokenFile-механики websockify в песочнице (vnc.html=200, валидный токен → 101 + backend connected, невалидный → разрыв). **Графическую часть (Camoufox в окне, ручной логин) проверяет пользователь на Debian** — в песочнице нет X-сервера/deb-репозитория |
| confirm-login → storage_state в БД **зашифрован** | ✅ тест читает сырые байты из SQLite: plaintext cookie/localStorage отсутствуют; расшифровка через Fernet OK |
| После рестарта сервера аккаунт восстанавливается без релогина | ✅ тест «restart» (browser shutdown → новый lifespan → контекст активного аккаунта жив, storage_state совпадает) |
| noVNC-сессия закрывается, браузер живёт | ✅ `confirm-login` → `vnc_manager.stop_session(keep_browser=True)` |
| Лимит 10 аккаунтов | ✅ тест |
| Изоляция пользователей (чужой аккаунт → 404) | ✅ тест |
| install.sh устанавливает стек | ✅ синтаксис проверен (`bash -n`); запуск — на целевом Debian (песочница без deb.debian.org) |
| VNC graceful-degrade без стека | ✅ live на 8123: `201 + vnc_error`, `503`, `409`, 401 без токена |

Прогон тестов: `9 passed` (`pytest tests/ -v`).

## Как проверить у себя на Debian 13 (всё делается скриптом, тебе руками кроме «Готово» ничего)

```bash
sudo bash deploy/install.sh                 # ставит xvfb x11vnc novnc websockify redis + venv + camoufox fetch
cd ai-dev-department/server
nano .env                                   # SERVICE_URL, PROXY_URL, ADMIN_PASSWORD (опц.)
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# В другом терминале:
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@local","password":"<ADMIN_PASSWORD>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8000/api/accounts -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"agent-1","default_role":"Coder","github_repos":["you/repo"]}'
# -> {"vnc_url": "http://<host>:6080/vnc.html?...token=...", ...}
# Открой vnc_url, войди в сервис вручную, затем:
curl -s -X POST localhost:8000/api/accounts/1/confirm-login -H "Authorization: Bearer $TOKEN"
# -> {"status":"active", ...}
sudo systemctl restart <uvicorn>   # или kill + перезапуск
curl -s localhost:8000/api/accounts -H "Authorization: Bearer $TOKEN"   # status: active после рестарта
```

Тесты (без графики): `pip install -r requirements-dev.txt && ./.venv/bin/python -m pytest tests/ -v`.

## Известные ограничения MVP (зафиксировано)

- noVNC-токен — JWT со сроком жизни 2 ч; websockify проверяет принадлежность к файлу токенов
  (не криптографию) — токен непредсказуем и удаляется при stop/confirm (D-032).
- IndexedDB в storage_state — best effort (playwright experimental API), см. D-035.
- Капча и ручной логин — только глазами через VNC (D-036).
- В песочнице нет X/deb-доступа: графическая приёмка — за пользователем на Debian (D-045).
