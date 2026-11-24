"""The API department end to end through the REST API, with a fake provider.

The lead (Orchestrator) plans, then gives work to a Coder and a Tester in ONE reply
(they run in parallel); the first key in the pool is rate-limited, so the very first
call moves to the second key. Also: stopping a run, roles and skills endpoints."""

from __future__ import annotations

import asyncio
import re
import time

import pytest

from app.agents import department, llm, skills as skills_mod
from app.agents.llm import LLMError, LLMResponse


def _call(tool, n=0, **args):
    return {"type": "tool_use", "id": f"{tool}-{n}", "name": tool, "input": args}


TIMES: dict[str, float] = {}


async def _script(role: str, turn: int, req) -> LLMResponse:
    if role == "Orchestrator":
        if "HANG" in req.messages[0]["content"]:
            await asyncio.sleep(30)
        if turn == 0:
            return LLMResponse([_call("TodoWrite", todos=[{"content": "code", "status": "in_progress"},
                                                         {"content": "tests", "status": "pending"}]),
                                _call("Task", 1, role="Coder", description="a.py", prompt="Write src/a.py"),
                                _call("Task", 2, role="Тестер", description="tests", prompt="Check the project")],
                               "tool_use", 100, 20)
        reports = [b["content"] for b in req.messages[-1]["content"]]
        assert any("Report from the Coder" in r for r in reports) and any("Report from the Tester" in r for r in reports)
        return LLMResponse([{"type": "text", "text": "All done: a.py written and checked."}], "end", 50, 10)
    if role == "Coder":
        if turn == 0:
            TIMES["coder_start"] = time.monotonic()
            for _ in range(100):  # wait for the Tester: only possible if both run at the same time
                if "tester_start" in TIMES:
                    break
                await asyncio.sleep(0.05)
            return LLMResponse([_call("Write", path="src/a.py", content="print('a')\n")], "tool_use", 30, 10)
        TIMES["coder_done"] = time.monotonic()
        return LLMResponse([{"type": "text", "text": "Wrote src/a.py"}], "end", 30, 5)
    if role == "Tester":
        if turn == 0:
            TIMES["tester_start"] = time.monotonic()
            return LLMResponse([_call("Bash", command="sleep 0.5; echo checked")], "tool_use", 20, 5)
        return LLMResponse([{"type": "text", "text": "Checked: OK"}], "end", 20, 5)
    raise AssertionError(role)


class FakeClient:
    def __init__(self, provider, secret, base_url):
        self.secret = secret

    async def probe(self, model):
        return None

    async def models(self):
        return ["m-1"]

    async def close(self):
        pass

    async def create(self, req):
        if self.secret.startswith("sk-limited"):
            raise LLMError(llm.RATE_LIMITED, "slow down", status=429, retry_after=60)
        role = re.search(r"You are the (\w+)", req.system).group(1)
        turn = sum(1 for m in req.messages if m["role"] == "assistant")
        return await _script(role, turn, req)


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "off")
    monkeypatch.setattr(department, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm, "CLIENT_FACTORY", FakeClient)
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}).json()[
            "access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        for key in client.get("/api/keys").json()["keys"]:
            client.delete(f"/api/keys/{key['id']}")
        for role in client.get("/api/agent-roles").json():
            client.delete(f"/api/agent-roles/{role['role']}")
        yield client


def _wait(api, run_id, done=("done", "failed", "stopped"), seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = api.get(f"/api/runs/{run_id}").json()
        if run["status"] in done and not run["live"]:
            return run
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} still {run['status']}")


def _setup_roles(api):
    assert api.post("/api/keys", json={"provider": "groq", "key": "sk-limited-1111"}).status_code == 201
    assert api.post("/api/keys", json={"provider": "groq", "key": "sk-good-2222"}).status_code == 201
    for role in ("Orchestrator", "Coder", "Tester"):
        r = api.put(f"/api/agent-roles/{role}", json={"provider": "groq", "model": "m-1", "effort": "low"})
        assert r.status_code == 200, r.text


def test_the_lead_gives_work_to_parallel_sub_agents_and_keys_rotate(api):
    TIMES.clear()
    _setup_roles(api)
    roles = {r["role"]: r for r in api.get("/api/agent-roles").json()}
    assert roles["Orchestrator"]["ready"] and roles["Coder"]["ready"] and not roles["Reviewer"]["ready"]
    assert "Task" in roles["Orchestrator"]["tools"] and "Bash" not in roles["Orchestrator"]["tools"]

    r = api.post("/api/runs", json={"prompt": "Make a tiny app with tests"})
    assert r.status_code == 201, r.text
    run = _wait(api, r.json()["id"])
    assert run["status"] == "done", run
    assert run["result"] == "All done: a.py written and checked."
    agents = {a["id"]: a for a in run["agents"]}
    assert agents["lead"]["role"] == "Orchestrator" and agents["lead.1"]["role"] == "Coder"
    assert agents["lead.2"]["role"] == "Tester"  # the Russian role name was understood
    assert all(a["status"] == "done" for a in agents.values()) and agents["lead.1"]["files"] == 1
    assert run["todos"][0] == {"content": "code", "status": "in_progress"}
    assert run["tokens_in"] >= 250
    # parallel: the Tester started before the Coder finished
    assert TIMES["tester_start"] < TIMES["coder_done"]

    events = api.get(f"/api/runs/{run['id']}/events").json()
    kinds = [e["type"] for e in events]
    assert "key_rotation" in kinds and kinds.count("agent_start") == 3
    rotation = next(e for e in events if e["type"] == "key_rotation")
    assert rotation["data"]["from_tail"] == "1111" and rotation["data"]["to_tail"] == "2222"
    keys = {k["tail"]: k for k in api.get("/api/keys").json()["keys"]}
    assert keys["1111"]["status"] == "limited" and keys["2222"]["status"] == "available"
    assert keys["2222"]["requests"] >= 6

    files = api.get(f"/api/runs/{run['id']}/files").json()
    assert [f["path"] for f in files] == ["src/a.py"]
    assert api.get(f"/api/runs/{run['id']}/file", params={"path": "src/a.py"}).text == "print('a')\n"
    assert api.get(f"/api/runs/{run['id']}/file", params={"path": "../../etc/passwd"}).status_code == 404
    archive = api.get(f"/api/runs/{run['id']}/archive")
    assert archive.status_code == 200 and archive.content[:2] == b"PK"
    after = events[-1]["id"]
    assert api.get(f"/api/runs/{run['id']}/events", params={"after": after}).json() == []


def test_a_run_can_be_stopped(api):
    _setup_roles(api)
    run_id = api.post("/api/runs", json={"prompt": "HANG forever"}).json()["id"]
    time.sleep(0.5)
    assert api.post(f"/api/runs/{run_id}/stop").json()["stopping"] is True
    run = _wait(api, run_id, seconds=5)
    assert run["status"] == "stopped"


def test_a_run_needs_a_ready_lead_role(api):
    r = api.post("/api/runs", json={"prompt": "do something"})
    assert r.status_code == 400 and "not set up" in r.json()["detail"]
    assert api.put("/api/agent-roles/Coder", json={"provider": "nope", "model": "m"}).status_code == 400
    assert api.put("/api/agent-roles/Coder", json={"provider": "groq", "model": "m", "tools": ["Hack"]}).status_code == 400


def test_skills_endpoints(api):
    names = [s["name"] for s in api.get("/api/skills").json()]
    assert {"code-review", "write-tests", "debugging", "frontend-design"} <= set(names)
    text = "---\nname: house-style\ndescription: Our code style.\n---\nUse tabs.\n"
    assert api.post("/api/skills", json={"text": text}).status_code == 201
    assert api.get("/api/skills/house-style").json()["body"] == "Use tabs."
    assert api.post("/api/skills", json={"text": "nope"}).status_code == 400
    up = api.post("/api/skills", files={"file": ("SKILL.md", text.replace("house-style", "uploaded"), "text/markdown")})
    assert up.status_code == 201 and up.json()["source"] == "user"
    assert api.delete("/api/skills/code-review").status_code == 404  # built-in skills stay
    assert api.delete("/api/skills/house-style").status_code == 204
