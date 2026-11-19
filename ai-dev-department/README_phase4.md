# Фаза 4 — Реальный адаптер ИИ-сервиса + подключение аккаунтов (README_phase4)

Фаза 3 заставила проекты ехать (оркестратор + github_watcher + WebSocket), но
адаптер внешнего ИИ-сервиса был stub с `NotImplementedError`. Фаза 4 делает
адаптер рабочим, подключает аккаунты к сервису через зашифрованный
`storage_state` и добавляет устойчивости оркестратору.

## Что добавлено

- `server/app/adapters/generic/adapter.py` — рабочий селекторный адаптер протокола
  `ServiceAdapter`: `open_chat`, `send_message`, `wait_response`, `get_state`,
  `is_alive`. Все DOM-селекторы (ввод, отправка, сообщения, состояние, логин)
  настраиваются через `ADAPTER_*` в конфиге; конкретный ИИ-сервис не зашит —
  только `SERVICE_URL`.
- `server/app/config.py` — настройки адаптера: `ADAPTER_INPUT_SELECTOR`,
  `ADAPTER_SEND_SELECTOR`, `ADAPTER_MESSAGE_SELECTOR`, `ADAPTER_STATE_SELECTOR`,
  `ADAPTER_LOGIN_SELECTOR`, `ADAPTER_READY_SELECTOR`,
  `ADAPTER_POLL_INTERVAL_SECONDS`, `ADAPTER_NAVIGATE_TIMEOUT_SECONDS`,
  `ADAPTER_MAX_RETRIES`, `ADAPTER_RETRY_BACKOFF_SECONDS`.
- `server/app/services/orchestrator.py` — `_ask` повторяет `open_chat` +
  `send_message` с экспоненциальным бэкоффом на временные ошибки;
  `wait_response` не повторяется (без дублирования запроса), но падает быстро
  на `error` / `login_required` / таймаут; `get_state` определяет
  `idle` / `generating` / `error` / `login_required`.
- `server/tests/test_adapter.py` — 15 тестов с фейковой DOM-страницей и
  изолированными настройками (без Camoufox, без реального сервиса).
- `DECISIONS.md` — D-059…D-064.

## Как аккаунт подключается к сервису

1. Пользователь создаёт аккаунт (`POST /api/accounts`) → `pending_login`.
2. Через VNC (`POST /api/accounts/{id}/vnc/start`) входит в ИИ-сервис вручную.
3. `POST /api/accounts/{id}/confirm-login` — `storage_state` браузера
   (куки + localStorage + IndexedDB) сохраняется Fernet-шифрованным,
   статус → `active`.
4. Оркестратор (Фаза 3) запускает браузер аккаунта через
   `browser_manager.launch(account_id, storage_state=...)`, адаптер работает
   в этом контексте — сервис видит сессию аккаунта. Сервер пароля не знает.

## Новые настройки (`.env`)

```
# Селекторы чата внешнего ИИ-сервиса (подправьте под его DOM)
ADAPTER_INPUT_SELECTOR=textarea, [contenteditable='true'], input[type='text']
ADAPTER_SEND_SELECTOR=button[type='submit'], [aria-label*='send' i]
ADAPTER_MESSAGE_SELECTOR=[role='article'], .message, .chat-message
ADAPTER_STATE_SELECTOR=[aria-busy='true'], .generating, .loading, .error
ADAPTER_LOGIN_SELECTOR=input[type='password'], [aria-label*='login' i]
ADAPTER_READY_SELECTOR=
# Тайминги и повторения
ADAPTER_POLL_INTERVAL_SECONDS=2.0
ADAPTER_NAVIGATE_TIMEOUT_SECONDS=30.0
ADAPTER_MAX_RETRIES=3
ADAPTER_RETRY_BACKOFF_SECONDS=2.0
```

## Acceptance criteria — что проверено и где

| Критерий | Статус |
|---|---|
| Адаптер загружается через `load_adapter()` и реализует все 5 методов протокола | ✅ `load_adapter()` возвращает `Adapter` |
| `open_chat` navigates на `SERVICE_URL/chat?ref=…` и ждёт ввода | ✅ тест `test_open_chat_*` |
| `send_message` заполняет ввод и нажимает отправку (fallback на Enter) | ✅ тесты `test_send_message_*` |
| `wait_response` возвращает ответ ассистента после перехода в `idle` | ✅ тест `test_wait_response_returns_assistant_message` |
| `get_state`: idle / generating / error / login_required | ✅ тесты `test_get_state_*` |
| `wait_response` падает на `error` / `login_required` / таймаут | ✅ тесты `test_wait_response_*_raises` |
| `is_alive` возвращает False при обрыве сети | ✅ тест `test_is_alive_returns_false_on_network_error` |
| Повторение `open_chat`+`send_message` с бэкоффом на временные ошибки | ✅ код `_ask` + `_retryable_sleep` |
| Всего тестов | ✅ `31 passed` (`pytest tests/ -v`) |

Прогон тестов: `31 passed` (`pytest tests/ -v`).

## Что дальше (Фаза 5 — не начата)

PySide6-клиент (рабочий стол) и финальный деплой. Статус: уточняется с пользователем.
