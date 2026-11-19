# Фаза 5 — PySide6-клиент (Windows) + финальный деплой (README_phase5)

Десктопный клиент для управления командой ИИ-агентов. Выполнено по варианту
«Ядро» (рекомендация принята пользователем): вход, список проектов, деталь
проекта с таймлайном задач. Аккаунты и VNC-просмотр отложены на последующие
итерации.

## Что добавлено

- `client/app/main.py` — точка входа PySide6 (`QApplication`, окно входа → главное окно).
- `client/app/api_client.py` — HTTP-клиент на `httpx`: Bearer-токен, авто-refresh
  при 401, все эндпоинты сервера (auth, accounts, projects, VNC).
- `client/app/auth_store.py` — токены только в памяти (без записи в QSettings).
- `client/app/windows/login.py` — вход / регистрация с инвайт-кодом.
- `client/app/windows/main.py` — список проектов (поиск, создание, старт/стоп/удаление).
- `client/app/windows/project_detail.py` — деталь проекта: статус, результат,
  таймлайн задач (роль, исполнитель, статус, ветка), кнопки старт/стоп/обновить.
- `client/build.spec` — PyInstaller-сборка standalone `.exe` для Windows.
- `client/requirements-client.txt` — `PySide6>=6.5.0`, `httpx>=0.27.0`.
- `DECISIONS.md` — D-065…D-067.

## Как запустить клиента

```bash
# на сервере должен चलваться uvicorn app.main:app (по умолчанию :8123)
cd client
../server/.venv/bin/pip install -r requirements-client.txt
API_BASE_URL=http://127.0.0.1:8123 ../server/.venv/bin/python -m app.main
```

Сборка `.exe` (на Windows, где установлен PySide6):

```bash
cd client
pyinstaller build.spec
# результат: dist/AI Dev Department Client.exe
```

## Acceptance criteria

| Критерий | Статус |
|---|---|
| Окно входа: логин + регистрация с инвайт-кодом | ✅ код + py_compile |
| Главное окно: список проектов, поиск, создание | ✅ код + py_compile |
| Деталь проекта: таймлайн задач, старт/стоп | ✅ код + py_compile |
| API-клиент: все эндпоинты, Bearer-токен, refresh при 401 | ✅ код + py_compile |
| PyInstaller build.spec | ✅ создан |
| Все файлы клиента компилируются | ✅ `py_compile` OK |

Графический запуск GUI требует Windows (или Linux с X-сервером + libGL) — в
песочнице без X-сервера проверка ограничивается `py_compile`.

## Что дальше

- Добавить окна аккаунтов и VNC-просмотра (запазданные части клиента).
- Финальный деплой: `deploy/install.sh` уже есть (сервер + venv + camoufox).
  systemd-юниты и cloudflared — по мере необходимости на целевом Debian.
