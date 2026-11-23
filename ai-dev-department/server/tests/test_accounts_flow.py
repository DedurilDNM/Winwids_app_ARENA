"""Phase 2 acceptance tests: accounts CRUD, VNC flow, storage-state encryption,
restore-after-restart, per-user isolation and the 10-account limit.

Camoufox is replaced with an in-memory fake - these tests verify OUR logic
(DB, encryption, state machine, API), not the browser itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import browser_manager as bm_module

TEST_DB = os.environ["TEST_DB_PATH"]


# ---------------------------------------------------------------- fake browser

class FakeResponse:
    status = 200


class FakePage:
    def __init__(self) -> None:
        self.url: str | None = None
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
        self.url = url
        return FakeResponse()

    async def close(self) -> None:
        self.closed = True


DEFAULT_STATE = {
    "cookies": [
        {"name": "session", "value": "session-cookie-value", "domain": "example.com", "path": "/"}
    ],
    "origins": [
        {"origin": "https://example.com",
         "localStorage": [{"name": "auth", "value": "local-storage-value"}]}
    ],
    "indexedDB": [{"name": "idb", "version": 1}],
}


class FakeContext:
    def __init__(self, storage_state: dict | None = None, **kwargs: Any) -> None:
        self.storage = dict(storage_state) if storage_state else json.loads(json.dumps(DEFAULT_STATE))
        self.closed = False
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def storage_state(self, indexed_db: bool | None = None, **kwargs: Any) -> dict:
        return json.loads(json.dumps(self.storage))

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kwargs: Any) -> FakeContext:
        ctx = FakeContext(storage_state=kwargs.get("storage_state"))
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        pass


class FakeCamoufox:
    """Drop-in replacement for AsyncCamoufox (headless test double)."""

    instances: list["FakeCamoufox"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.browser: FakeBrowser | None = None
        FakeCamoufox.instances.append(self)

    async def start(self) -> FakeBrowser:
        self.browser = FakeBrowser()
        return self.browser

    async def stop(self) -> None:
        pass


@pytest.fixture()
def api(monkeypatch):
    FakeCamoufox.instances = []
    monkeypatch.setattr(bm_module, "AsyncCamoufox", FakeCamoufox)
    with TestClient(app) as client:
        yield client


def _login_admin(client: TestClient) -> str:
    resp = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_account(client: TestClient, token: str, name: str = "agent-1") -> dict:
    resp = client.post(
        "/api/accounts",
        json={"name": name, "default_role": "Coder", "github_repos": ["octo/repo"]},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------- tests

def test_health_and_auth_required(api):
    assert api.get("/health").json() == {"status": "ok"}
    resp = api.get("/api/accounts")
    assert resp.status_code in (401, 403)


def test_create_account_creates_pending_login_and_launches_browser(api):
    token = _login_admin(api)
    body = _create_account(api, token)
    assert body["status"] == "pending_login"
    assert body["headless"] is True  # VNC_HEADLESS=true in tests
    assert body["vnc_url"] is None
    # Browser was launched against SERVICE_URL
    assert FakeCamoufox.instances, "expected a browser launch"


def test_confirm_login_saves_encrypted_storage_state(api):
    token = _login_admin(api)
    account = _create_account(api, token)

    resp = api.post(f"/api/accounts/{account['id']}/confirm-login", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    raw = sqlite3.connect(TEST_DB).execute(
        "SELECT storage_state_encrypted FROM accounts WHERE id = ?", (account["id"],)
    ).fetchone()[0]
    assert raw, "storage_state_encrypted must not be empty"
    assert b"session-cookie-value" not in raw  # encrypted at rest
    assert b"local-storage-value" not in raw

    from app.security import decrypt_bytes

    state = json.loads(decrypt_bytes(raw))
    assert state["cookies"][0]["value"] == "session-cookie-value"
    assert state["origins"][0]["localStorage"][0]["value"] == "local-storage-value"


def test_confirm_login_without_live_session_returns_409(api):
    token = _login_admin(api)
    account = _create_account(api, token)
    asyncio.run(bm_module.browser_manager.stop(account["id"]))  # simulate browser crash

    resp = api.post(f"/api/accounts/{account['id']}/confirm-login", headers=_auth(token))
    assert resp.status_code == 409


def test_restart_restores_active_accounts(api, monkeypatch):
    token = _login_admin(api)
    account = _create_account(api, token)
    api.post(f"/api/accounts/{account['id']}/confirm-login", headers=_auth(token))

    # Simulate a full server restart: all browsers die, then the lifespan relaunches them.
    asyncio.run(bm_module.browser_manager.shutdown())
    instances_before = len(FakeCamoufox.instances)
    with TestClient(app) as second:
        restored_context = bm_module.browser_manager.get_context(account["id"])
        assert restored_context is not None, "active account must be restored on startup"
        restored_state = restored_context.storage
        assert restored_state["cookies"][0]["value"] == "session-cookie-value"
    assert len(FakeCamoufox.instances) > instances_before


def test_vnc_start_unavailable_returns_503(api, monkeypatch):
    token = _login_admin(api)
    account = _create_account(api, token)
    # Simulate the live session being gone (e.g. after a crash).
    from app.services import vnc_manager as vnc_module

    asyncio.run(vnc_module.vnc_manager.stop_session(account["id"], keep_browser=False))
    # Simulate missing Camoufox installation.
    monkeypatch.setattr(bm_module, "AsyncCamoufox", None)
    resp = api.post(f"/api/accounts/{account['id']}/vnc/start", headers=_auth(token))
    assert resp.status_code == 503


def test_accounts_are_isolated_per_user(api):
    token = _login_admin(api)
    account = _create_account(api, token)
    token2 = _register_user(api, "user2@example.com", "invite-for-user2")

    assert api.get(f"/api/accounts/{account['id']}", headers=_auth(token2)).status_code == 404
    assert api.get("/api/accounts", headers=_auth(token2)).json() == []
    assert api.delete(f"/api/accounts/{account['id']}", headers=_auth(token2)).status_code == 404


def _register_user(api, email: str, invite_code: str) -> str:
    from app.db import SessionLocal
    from app.models import Invite

    async def _insert_invite() -> None:
        async with SessionLocal() as session:
            session.add(Invite(code=invite_code))
            await session.commit()

    asyncio.run(_insert_invite())
    resp = api.post(
        "/api/auth/register",
        json={"email": email, "password": "password-12345", "invite_code": invite_code},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def test_account_limit_is_10_per_user(api):
    token = _register_user(api, "limit@example.com", "invite-limit-user")
    for i in range(1, 11):
        _create_account(api, token, f"agent-{i}")
    resp = api.post(
        "/api/accounts",
        json={"name": "agent-11", "default_role": "Coder", "github_repos": []},
        headers=_auth(token),
    )
    assert resp.status_code == 403


def test_update_and_delete_account(api):
    token = _login_admin(api)
    account = _create_account(api, token)

    resp = api.patch(
        f"/api/accounts/{account['id']}",
        json={"default_role": "Reviewer", "github_repos": ["octo/a", "octo/b"]},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["default_role"] == "Reviewer"
    assert resp.json()["github_repos"] == ["octo/a", "octo/b"]

    resp = api.delete(f"/api/accounts/{account['id']}", headers=_auth(token))
    assert resp.status_code == 204
    assert api.get(f"/api/accounts/{account['id']}", headers=_auth(token)).status_code == 404


def test_stale_x_display_files_are_detected_and_cleared(tmp_path, monkeypatch):
    """A socket file without a live X server must not count as a running display."""
    import socket as _socket
    from pathlib import Path as _Path

    from app.services.vnc_manager import VncManager

    display = 42999  # never used by a real server
    sock_path = _Path(f"/tmp/.X11-unix/X{display}")
    lock_path = _Path(f"/tmp/.X{display}-lock")
    sock_path.parent.mkdir(exist_ok=True)
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        srv.listen(1)
        assert VncManager.x_display_alive(display) is True   # something answers
        srv.close()                                           # server "crashed", file stays
        assert sock_path.exists() and VncManager.x_display_alive(display) is False
        lock_path.write_text("12345")
        VncManager._clear_stale_display(display)
        assert not sock_path.exists() and not lock_path.exists()
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def test_login_credentials_are_write_only_and_encrypted(api):
    token = _login_admin(api)
    account = _create_account(api, token, name="cred-1")
    assert account["has_login_password"] is False and account["login_email"] is None

    resp = api.patch(f"/api/accounts/{account['id']}", headers=_auth(token),
                     json={"login_password": "Sup3r-Secret!", "service_note": "почта: bot.one@example.com"})
    body = resp.json()
    assert resp.status_code == 200 and body["has_login_password"] is True
    assert "Sup3r-Secret!" not in resp.text and "login_password" not in body
    raw = sqlite3.connect(TEST_DB).execute(
        "SELECT login_password_encrypted FROM accounts WHERE id = ?", (account["id"],)).fetchone()[0]
    assert raw and b"Sup3r-Secret!" not in raw

    from app.models import Account
    from app.services.credentials import login_credentials

    row = Account(service_note="почта: bot.one@example.com", login_password_encrypted=raw)
    assert login_credentials(row) == ("bot.one@example.com", "Sup3r-Secret!")  # e-mail from the note
    row.login_email = "explicit@example.com"
    assert login_credentials(row)[0] == "explicit@example.com"

    # omitted password = unchanged; "" = deleted
    assert api.patch(f"/api/accounts/{account['id']}", headers=_auth(token),
                     json={"login_email": "x@example.com"}).json()["has_login_password"] is True
    body = api.patch(f"/api/accounts/{account['id']}", headers=_auth(token), json={"login_password": ""}).json()
    assert body["has_login_password"] is False and body["login_email"] == "x@example.com"


def test_calibration_api_runs_in_background_and_shows_booleans_only(api, monkeypatch):
    import time as _time

    import app.api.calibrate as cal

    token = _login_admin(api)
    account = _create_account(api, token, name="cal-1")
    secret_selector = 'div[data-author="assistant"]'

    async def fake_run(acc, progress=None):
        if progress:
            await progress("waiting for the answer")
        from app.adapters.generic.calibrate import apply_profile
        from app.db import SessionLocal

        profile = {"ok": True, "reader": "selector", "message": secret_selector, "busy": "button.stop",
                   "checks": {"markers": True, "code_block": True}, "modality": "default", "checked_at": "2026-11-22T20:00:00",
                   "seconds": 9.1}
        await apply_profile(SessionLocal, acc.id, profile)
        return profile

    monkeypatch.setattr(cal, "run_calibration", fake_run)
    resp = api.post(f"/api/accounts/{account['id']}/calibrate", headers=_auth(token))
    assert resp.status_code == 202 and resp.json()["running"] is True
    for _ in range(50):
        st = api.get(f"/api/accounts/{account['id']}/calibrate", headers=_auth(token)).json()
        if not st["running"]:
            break
        _time.sleep(0.1)
    assert st["result"]["ok"] is True and st["result"]["busy_found"] is True
    assert secret_selector not in str(st)  # selectors never leave the server
    card = next(a for a in api.get("/api/accounts", headers=_auth(token)).json() if a["id"] == account["id"])
    assert card["calibration"]["ok"] is True and card["calibration"]["reader"] == "selector"
    assert secret_selector not in str(card)


def test_confirm_login_never_fails_with_a_bare_500(api, monkeypatch):
    from app.services.browser_manager import browser_manager

    token = _login_admin(api)
    account = _create_account(api, token, name="save-1")

    async def broken(account_id):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(browser_manager, "export_storage_state", broken)
    resp = api.post(f"/api/accounts/{account['id']}/confirm-login", headers=_auth(token))
    assert resp.status_code == 409 and "Открыть браузер" in resp.json()["detail"]


def test_storage_state_falls_back_when_indexeddb_export_fails():
    import asyncio as _asyncio
    from types import SimpleNamespace

    from app.services.browser_manager import BrowserManager

    class Ctx:
        async def storage_state(self, **kw):
            if kw.get("indexed_db"):
                raise RuntimeError("IndexedDB export failed")
            return {"cookies": [{"name": "s", "value": "1"}], "origins": []}

    bm = BrowserManager()
    bm._handles[77] = SimpleNamespace(context=Ctx())
    state = _asyncio.run(bm.export_storage_state(77))
    assert state["cookies"][0]["value"] == "1"
