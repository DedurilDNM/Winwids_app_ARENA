"""Phase 3 acceptance tests: projects CRUD, start validation, WebSocket snapshot.

Camoufox is faked (mirrors tests/test_accounts_flow.py). Projects do not
launch a browser until they are started, so these tests are pure API + DB
checks, plus the orchestrator plan-parser unit tests.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import browser_manager as bm_module

TEST_DB = os.environ["TEST_DB_PATH"]


# ---------------------------------------------------------------- fake browser + client fixture


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self) -> None:
        self.url: str | None = None
        self.closed = False

    async def goto(self, url: str, **kwargs: object) -> FakeResponse:
        self.url = url
        return FakeResponse()

    async def close(self) -> None:
        self.closed = True


DEFAULT_STATE = {
    "cookies": [
        {"name": "session", "value": "session-cookie-value", "domain": "example.com", "path": "/"}
    ],
    "origins": [
        {"origin": "https://example.com", "localStorage": [{"name": "auth", "value": "local-storage-value"}]}
    ],
    "indexedDB": [{"name": "idb", "version": 1}],
}


class FakeContext:
    def __init__(self, storage_state: dict | None = None, **kwargs: object) -> None:
        self.storage = dict(storage_state) if storage_state else json.loads(json.dumps(DEFAULT_STATE))
        self.closed = False
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def storage_state(self, indexed_db: bool | None = None, **kwargs: object) -> dict:
        return json.loads(json.dumps(self.storage))

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kwargs: object) -> FakeContext:
        ctx = FakeContext(storage_state=kwargs.get("storage_state"))
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        pass


class FakeCamoufox:
    """Drop-in replacement for AsyncCamoufox (headless test double)."""

    instances: list["FakeCamoufox"] = []

    def __init__(self, **kwargs: object) -> None:
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


# ---------------------------------------------------------------- helpers


def _login_admin(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _fresh_user(client: TestClient, suffix: str) -> str:
    """Register a dedicated user so per-user account limits aren't exhausted across tests."""
    email = f"proj-{suffix}@example.com"
    code = f"invite-proj-{suffix}"
    return _register_user(client, email, code)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(client: TestClient, email: str, invite_code: str) -> str:
    async def _insert() -> None:
        from app.db import SessionLocal
        from app.models import Invite

        async with SessionLocal() as session:
            session.add(Invite(code=invite_code))
            await session.commit()

    asyncio.run(_insert())
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password-12345", "invite_code": invite_code},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _create_account(client: TestClient, token: str, name: str = "agent-1") -> dict:
    resp = client.post(
        "/api/accounts",
        json={"name": name, "default_role": "Coder", "github_repos": ["octo/repo"]},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activate(client: TestClient, token: str, account_id: int) -> None:
    resp = client.post(f"/api/accounts/{account_id}/confirm-login", headers=_auth(token))
    assert resp.status_code == 200, resp.text


def _project_payload(**overrides) -> dict:
    payload = {
        "name": "Website",
        "description": "A demo project",
        "task": "Build a landing page",
        "github_repo": "octo/website",
        "orchestrator_account_id": None,
        "assignments": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- CRUD


def test_health_ok(api):  # noqa: F811
    assert api.get("/health").json() == {"status": "ok"}


def test_projects_crud_isolated(api):  # noqa: F811
    token = _fresh_user(api, "crud")
    account = _create_account(api, token)

    resp = api.post(
        "/api/projects",
        json=_project_payload(
            orchestrator_account_id=account["id"],
            assignments=[{"account_id": account["id"], "role": "Coder"}],
        ),
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    project = resp.json()
    assert project["status"] == "draft"
    assert project["assignments"] == [{"account_id": account["id"], "role": "Coder"}]
    pid = project["id"]

    resp = api.get("/api/projects", headers=_auth(token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = api.get(f"/api/projects/{pid}", headers=_auth(token))
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == pid
    assert detail["tasks"] == []

    token2 = _register_user(api, "other@example.com", "invite-other")
    assert api.get(f"/api/projects/{pid}", headers=_auth(token2)).status_code == 404
    assert len(api.get("/api/projects", headers=_auth(token2)).json()) == 0

    resp = api.patch(f"/api/projects/{pid}", json={"name": "Website v2"}, headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["name"] == "Website v2"

    resp = api.delete(f"/api/projects/{pid}", headers=_auth(token))
    assert resp.status_code == 204
    assert api.get(f"/api/projects/{pid}", headers=_auth(token)).status_code == 404


def test_project_patch_rejects_running(api, monkeypatch):  # noqa: F811
    # Prevent the inline arq worker from running (no Redis in tests) so the
    # project stays "running" long enough to exercise stop/delete.
    import app.api.projects as projects_api_module

    async def _noop_enqueue(pid: int) -> str:
        return "inline"

    monkeypatch.setattr(projects_api_module, "enqueue_run_project", _noop_enqueue)

    token = _fresh_user(api, "patch")
    account = _create_account(api, token)
    _activate(api, token, account["id"])

    resp = api.post(
        "/api/projects",
        json=_project_payload(
            orchestrator_account_id=account["id"],
            assignments=[{"account_id": account["id"], "role": "Coder"}],
        ),
        headers=_auth(token),
    )
    pid = resp.json()["id"]

    resp = api.post(f"/api/projects/{pid}/start", headers=_auth(token))
    assert resp.status_code == 202, resp.text

    resp = api.patch(f"/api/projects/{pid}", json={"name": "X"}, headers=_auth(token))
    assert resp.status_code == 409

    resp = api.post(f"/api/projects/{pid}/stop", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"

    resp = api.delete(f"/api/projects/{pid}", headers=_auth(token))
    assert resp.status_code == 204


def test_start_validation(api):  # noqa: F811
    token = _fresh_user(api, "start")
    account = _create_account(api, token)
    _activate(api, token, account["id"])

    # no assignments -> 400
    resp = api.post(
        "/api/projects",
        json=_project_payload(orchestrator_account_id=account["id"]),
        headers=_auth(token),
    )
    pid = resp.json()["id"]
    resp = api.post(f"/api/projects/{pid}/start", headers=_auth(token))
    assert resp.status_code == 400, resp.text

    # orchestrator not yet active (pending_login) -> 409
    inactive = _create_account(api, token, "inactive-agent")
    resp = api.post(
        "/api/projects",
        json=_project_payload(
            orchestrator_account_id=inactive["id"],
            assignments=[{"account_id": inactive["id"], "role": "Coder"}],
        ),
        headers=_auth(token),
    )
    pid = resp.json()["id"]
    resp = api.post(f"/api/projects/{pid}/start", headers=_auth(token))
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------- WebSocket


def test_ws_snapshot_and_auth(api):  # noqa: F811
    token = _fresh_user(api, "ws")
    account = _create_account(api, token)
    resp = api.post(
        "/api/projects",
        json=_project_payload(
            orchestrator_account_id=account["id"],
            assignments=[{"account_id": account["id"], "role": "Coder"}],
        ),
        headers=_auth(token),
    )
    pid = resp.json()["id"]

    # owner with valid token -> snapshot
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/projects/{pid}?token={token}") as ws:
            data = ws.receive_json()
            assert data["type"] == "snapshot"
            assert data["project_id"] == pid
            assert data["project"]["status"] == "draft"
            assert data["project"]["assignments"] == [{"account_id": account["id"], "role": "Coder"}]
            assert data["tasks"] == []

    # foreign user token -> rejected (server closes the socket without a snapshot)
    from starlette.websockets import WebSocketDisconnect

    token2 = _register_user(api, "ws-other@example.com", "invite-ws-other")
    rejected = False
    try:
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/projects/{pid}?token={token2}") as ws:
                ws.receive_json()
    except (WebSocketDisconnect, Exception):
        rejected = True
    assert rejected, "foreign user must be rejected on WS connect"


# ---------------------------------------------------------------- plan parser


def test_parse_task_plan_happy_path():
    from app.services.orchestrator import parse_task_plan
    from app.models import ProjectAssignment

    assignments = [
        ProjectAssignment(id=1, project_id=1, account_id=10, role="Coder"),
        ProjectAssignment(id=2, project_id=1, account_id=11, role="Reviewer"),
    ]
    plan = parse_task_plan(
        '[{"role": "Coder", "task": "build UI", "assign_to_account_id": 10}]',
        assignments=assignments,
        orchestrator_account_id=10,
    )
    assert len(plan) == 1
    assert plan[0].role == "Coder"
    assert plan[0].task == "build UI"
    assert plan[0].assign_to_account_id == 10


def test_parse_task_plan_skips_invalid_and_raises_when_empty():
    from app.services.orchestrator import parse_task_plan
    from app.models import ProjectAssignment

    assignments = [ProjectAssignment(id=1, project_id=1, account_id=10, role="Coder")]

    with pytest.raises(ValueError):
        parse_task_plan("not json at all", assignments=assignments, orchestrator_account_id=10)

    with pytest.raises(ValueError):
        parse_task_plan(
            '[{"role": "Janitor", "task": "clean", "assign_to_account_id": 10}]',
            assignments=assignments,
            orchestrator_account_id=10,
        )

    plan = parse_task_plan(
        "```json\n[{\"role\": \"Coder\", \"task\": \"do it\", \"assign_to_account_id\": 10}]\n```",
        assignments=assignments,
        orchestrator_account_id=10,
    )
    assert len(plan) == 1
    assert plan[0].task == "do it"


def test_parse_task_plan_role_fallback_to_orchestrator():
    from app.services.orchestrator import parse_task_plan
    from app.models import ProjectAssignment

    assignments = [ProjectAssignment(id=1, project_id=1, account_id=10, role="Coder")]
    plan = parse_task_plan(
        '[{"role": "Coder", "task": "do it", "assign_to_account_id": 999}]',
        assignments=assignments,
        orchestrator_account_id=10,
    )
    assert len(plan) == 1
    assert plan[0].assign_to_account_id == 10


def test_ws_auth_by_first_message(api):  # noqa: F811
    token = _fresh_user(api, "wsmsg")
    pid = api.post("/api/projects", json=_project_payload(), headers=_auth(token)).json()["id"]
    with api.websocket_connect(f"/ws/projects/{pid}") as ws:
        ws.send_json({"type": "auth", "token": token})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["project"]["id"] == pid
        assert "pr_url" in snap["project"]
    other = _fresh_user(api, "wsmsg2")
    with api.websocket_connect(f"/ws/projects/{pid}") as ws:
        ws.send_json({"type": "auth", "token": other})
        try:
            ws.receive_json()
            raise AssertionError("foreign user must be rejected")
        except Exception as exc:  # WebSocketDisconnect
            assert getattr(exc, "code", 4401) == 4401


def test_interrupted_projects_marked_failed_on_startup(api):  # noqa: F811
    import asyncio as _asyncio

    from app.db import SessionLocal
    from app.main import fail_interrupted_projects
    from app.models import Project

    token = _fresh_user(api, "restart")
    pid = api.post("/api/projects", json=_project_payload(), headers=_auth(token)).json()["id"]

    async def mark_running():
        async with SessionLocal() as s:
            (await s.get(Project, pid)).status = "running"
            await s.commit()

    _asyncio.run(mark_running())
    assert _asyncio.run(fail_interrupted_projects()) >= 1
    body = api.get(f"/api/projects/{pid}", headers=_auth(token)).json()
    assert body["status"] == "failed" and "restart" in body["result"]
