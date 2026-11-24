"""Chats with the API department: turns, follow-ups with history, modes, model choice,
changes per turn and per chat, files - through the REST API with a fake provider."""

from __future__ import annotations

import asyncio
import re
import subprocess
import time

import pytest

from app.agents import department, llm, skills as skills_mod
from app.agents.llm import LLMResponse


def _call(tool, n=0, **args):
    return {"type": "tool_use", "id": f"{tool}-{n}-{time.monotonic_ns()}", "name": tool, "input": args}


def _this_turn(messages):
    """Assistant replies since the latest user message with text (the current prompt)."""
    last_prompt = max(i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str))
    return sum(1 for m in messages[last_prompt:] if m["role"] == "assistant"), messages[last_prompt]["content"]


class Fake:
    seen: list[dict] = []
    hold = None  # an asyncio.Event the lead waits for (to test «busy»)

    def __init__(self, provider, secret, base_url):
        self.provider = provider

    async def close(self):
        pass

    async def probe(self, model):
        return None

    async def models_meta(self):
        return [{"id": "m-1", "price_out": 5.0, "context": 100000, "tools": True, "kind": ""}]

    async def create(self, req):
        role = re.search(r"You are the (\w+)", req.system).group(1)
        turn, prompt = _this_turn(req.messages)
        Fake.seen.append({"role": role, "model": req.model, "tools": [t["name"] for t in req.tools],
                          "messages": len(req.messages), "prompt": prompt, "system": req.system,
                          "history": [m["content"] if isinstance(m["content"], str) else m["content"][0].get("text")
                                      for m in req.messages[:-1] if m["role"] in ("user", "assistant") and turn == 0]})
        if "WAIT" in prompt and Fake.hold is not None:
            await Fake.hold.wait()
        if role == "Orchestrator":
            if "plan only" in prompt or "PLAN" in prompt:
                return LLMResponse([{"type": "text", "text": "1. Do X\n2. Do Y"}], "end", 5, 5)
            if turn == 0:
                return LLMResponse([_call("Task", role="Coder", prompt=f"Do this: {prompt}")], "tool_use", 10, 5)
            return LLMResponse([{"type": "text", "text": f"Done: {prompt}"}], "end", 10, 5)
        if role == "Coder":
            if turn == 0:
                if "dark theme" in prompt:
                    return LLMResponse([_call("Edit", path="app.css", old_string="color: black",
                                              new_string="color: white")], "tool_use", 5, 5)
                return LLMResponse([_call("Write", path="app.css", content="body {\n  color: black\n}\n"),
                                    _call("Write", path="index.html", content="<h1>hi</h1>\n", n=2)], "tool_use", 5, 5)
            return LLMResponse([{"type": "text", "text": "coded"}], "end", 5, 5)
        raise AssertionError(role)


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "off")
    monkeypatch.setattr(department, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm, "CLIENT_FACTORY", Fake)
    Fake.seen, Fake.hold = [], None
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}).json()[
            "access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        for key in client.get("/api/keys").json()["keys"]:
            client.delete(f"/api/keys/{key['id']}")
        for role in client.get("/api/agent-roles").json():
            client.delete(f"/api/agent-roles/{role['role']}")
        for chat in client.get("/api/chats").json():
            client.delete(f"/api/chats/{chat['id']}")
        assert client.post("/api/keys", json={"provider": "groq", "key": "sk-groq-1111"}).status_code == 201
        for role in ("Orchestrator", "Coder"):
            client.put(f"/api/agent-roles/{role}", json={"provider": "groq", "model": "m-1"})
        yield client


def _idle(api, chat_id, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chat = api.get(f"/api/chats/{chat_id}").json()
        if chat["status"] != "running":
            return chat
        time.sleep(0.15)
    raise AssertionError(chat)


def test_a_chat_with_a_follow_up_keeps_the_conversation_and_the_files(api):
    r = api.post("/api/chats", json={"prompt": "Make a page with a black text"})
    assert r.status_code == 201, r.text
    chat_id = r.json()["id"]
    chat = _idle(api, chat_id)
    t1 = chat["turns"][0]
    assert t1["status"] == "done" and t1["result"] == "Done: Make a page with a black text"
    assert [a["role"] for a in t1["agents"]] == ["Orchestrator", "Coder"]
    assert chat["title"].startswith("Make a page") and api.get("/api/chats").json()[0]["id"] == chat_id

    changes = api.get(f"/api/chats/{chat_id}/changes", params={"run": t1["id"]}).json()
    assert [(f["path"], f["added"]) for f in changes["files"]] == [("app.css", 3), ("index.html", 1)]

    # the follow-up: the lead sees the earlier message and answer; the same files are changed
    r = api.post(f"/api/chats/{chat_id}/messages", json={"prompt": "Now a dark theme please"})
    assert r.status_code == 201, r.text
    chat = _idle(api, chat_id)
    t2 = chat["turns"][1]
    assert t2["status"] == "done" and t2["result"] == "Done: Now a dark theme please"
    lead_first = next(s for s in Fake.seen if s["role"] == "Orchestrator" and s["prompt"] == "Now a dark theme please")
    assert lead_first["history"][:2] == ["Make a page with a black text", "Done: Make a page with a black text"]

    turn2 = api.get(f"/api/chats/{chat_id}/changes", params={"run": t2["id"]}).json()
    assert [(f["path"], f["added"], f["deleted"]) for f in turn2["files"]] == [("app.css", 1, 1)]
    whole = api.get(f"/api/chats/{chat_id}/changes").json()
    assert {f["path"] for f in whole["files"]} == {"app.css", "index.html"}
    diff = api.get(f"/api/chats/{chat_id}/diff", params={"path": "app.css", "run": t2["id"]}).text
    assert "-  color: black" in diff and "+  color: white" in diff
    assert api.get(f"/api/chats/{chat_id}/file", params={"path": "app.css"}).text == "body {\n  color: white\n}\n"
    html = api.get(f"/api/chats/{chat_id}/file", params={"path": "index.html"})
    assert "sandbox" in html.headers["content-security-policy"]  # a prototype cannot act as the app
    files = [f["path"] for f in api.get(f"/api/chats/{chat_id}/files").json()]
    assert files == ["app.css", "index.html"]  # the snapshot store is not a project file
    assert api.get(f"/api/chats/{chat_id}/file", params={"path": ".aidd-git/HEAD"}).status_code == 404

    events = api.get(f"/api/chats/{chat_id}/events").json()
    edit = next(e for e in events if e["type"] == "tool_done" and e["data"]["tool"] == "Edit")
    assert edit["data"]["diff"] == ["-color: black", "+color: white"] and edit["run_id"] == t2["id"]


def test_modes_and_a_chosen_model(api):
    chat_id = api.post("/api/chats", json={"prompt": "Write the page", "mode": "single",
                                           "model": "groq:big-model"}).json()["id"]
    chat = _idle(api, chat_id)
    assert chat["turns"][0]["status"] == "done" and chat["turns"][0]["mode"] == "single"
    single = [s for s in Fake.seen if s["prompt"] == "Write the page"]
    assert single[0]["role"] == "Coder" and single[0]["model"] == "big-model" and "Task" not in single[0]["tools"]
    assert [a["role"] for a in chat["turns"][0]["agents"]] == ["Coder"]  # no sub-agents

    api.post(f"/api/chats/{chat_id}/messages", json={"prompt": "PLAN the next step", "mode": "plan"})
    chat = _idle(api, chat_id)
    plan = next(s for s in Fake.seen if s["prompt"] == "PLAN the next step")
    assert plan["role"] == "Orchestrator" and not {"Write", "Edit", "Bash", "Task"} & set(plan["tools"])
    assert "planning only" in plan["system"]
    assert api.post("/api/chats", json={"prompt": "x", "model": "no-colon"}).status_code == 400


def test_a_busy_chat_refuses_a_second_message_and_can_be_stopped_and_deleted(api):
    async def make_hold():
        return asyncio.Event()

    Fake.hold = asyncio.Event()  # never set: the lead waits until stopped
    chat_id = api.post("/api/chats", json={"prompt": "WAIT for me"}).json()["id"]
    time.sleep(0.5)
    assert api.get(f"/api/chats/{chat_id}").json()["status"] == "running"
    assert api.post(f"/api/chats/{chat_id}/messages", json={"prompt": "more"}).status_code == 409
    assert api.post(f"/api/chats/{chat_id}/stop").json()["stopping"] is True
    chat = _idle(api, chat_id, seconds=5)
    assert chat["turns"][0]["status"] == "stopped"
    ws = department.chat_workspace(chat_id)
    assert ws.exists()
    assert api.delete(f"/api/chats/{chat_id}").status_code == 204
    assert not ws.exists() and api.get(f"/api/chats/{chat_id}").status_code == 404


def test_the_projects_own_git_history_is_never_touched(api, tmp_path):
    chat_id = api.post("/api/chats", json={"prompt": "PLAN only, look around"}).json()["id"]
    _idle(api, chat_id)
    ws = department.chat_workspace(chat_id)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "README.md").write_text("project\n")
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "init"], cwd=ws, check=True)
    api.post(f"/api/chats/{chat_id}/messages", json={"prompt": "Make it"})
    _idle(api, chat_id)
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout.splitlines()
    assert len(log) == 1 and log[0].endswith("init")  # only the project's own commit
    changes = api.get(f"/api/chats/{chat_id}/changes").json()
    assert {"app.css", "index.html", "README.md"} <= {f["path"] for f in changes["files"]}
    assert not any(f["path"].startswith(".git/") for f in changes["files"])
