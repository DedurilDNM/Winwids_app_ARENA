# Client (PySide6, Windows)

```bash
pip install -r requirements-client.txt        # full PySide6 (includes QtWebEngine)
API_BASE_URL=http://127.0.0.1:8000 python -m app.main
pyinstaller build.spec                        # Windows .exe
```

Windows:
- `windows/login.py` — login / registration by invite code.
- `windows/main.py` — tabs «Проекты» and «Аккаунты».
- `windows/project_detail.py` — project task timeline, start/stop.
- `windows/accounts_tab.py` — account list (limit 10), add / VNC login /
  disable / delete.
- `windows/vnc_view.py` — embedded noVNC viewer (`QWebEngineView`) and the
  modal «Я вошёл / Готово» dialog. Without QtWebEngine it falls back to
  opening noVNC in the system browser.

Add-account flow: `POST /api/accounts` → `POST /api/accounts/{id}/vnc/start`
→ manual login in the server's browser over noVNC → «Я вошёл / Готово» →
`POST /api/accounts/{id}/confirm-login` → list refreshes.

Network calls run in background threads (`widgets/api_worker.py`); every
failure, including "server unreachable", arrives as `ApiError`
(`status_code == 0` for network errors). Expired access tokens are refreshed
automatically via `POST /api/auth/refresh`.
