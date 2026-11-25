"""Claude Code as the engine: the model proxy over the key pool, the stream-json -> chat events
translation, the sandbox, skills and agents, and the engine choice. (The real CLI end to end:
web/tests/e2e-engine.mjs.)"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agents.runtime import AgentSpec
from app.engine import claude_code, proxy

from tests.test_keypool import api  # noqa: F401  (the logged-in client fixture)


def _sse(text="hi", inp=200, out=40) -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": inp, "output_tokens": 1}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": out}}),
        ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {k}\ndata: {json.dumps(v, ensure_ascii=False)}\n\n" for k, v in events).encode()


def _add_key(api, secret, base="https://agg.example/v1"):
    from app.agents import llm

    def ok(request):
        return httpx.Response(200, json={"data": [{"id": "cc/claude-opus-5-5"}]})

    transport = httpx.MockTransport(ok)
    orig = llm.CLIENT_FACTORY
    llm.CLIENT_FACTORY = lambda provider, s, b: llm.OpenAICompatible(provider, s, b, transport=transport)
    try:
        r = api.post("/api/keys", json={"provider": "custom", "key": secret, "base_url": base})
    finally:
        llm.CLIENT_FACTORY = orig
    assert r.status_code == 201, r.text
    return r.json()


def test_the_proxy_streams_through_the_pool_and_rotates_limited_keys(api, monkeypatch):
    _add_key(api, "sk-first-1111")
    _add_key(api, "sk-second-2222")
    seen = []

    def upstream(request):
        key = request.headers["x-api-key"]
        seen.append((str(request.url), key[-4:], request.headers.get("anthropic-beta"), json.loads(request.content)["model"]))
        if key.endswith("1111"):
            return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
                                  headers={"retry-after": "60"})
        return httpx.Response(200, content=_sse("привет"), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(proxy, "HTTP_TRANSPORT", httpx.MockTransport(upstream))
    me = api.get("/api/auth/me").json()
    access = proxy.RunAccess(user_id=me["id"], run_id=1, models={"cc/claude-opus-5-5": "custom"}, default_provider="custom")
    token = proxy.open_access(access)
    try:
        for _ in range(2):  # whichever key comes first, the answer arrives
            r = api.post(f"/api/llm-proxy/{token}/v1/messages?beta=true",
                         json={"model": "cc/claude-opus-5-5", "stream": True, "messages": []},
                         headers={"anthropic-beta": "claude-code-20250219", "anthropic-version": "2023-06-01",
                                  "Authorization": "Bearer ignored"})
            assert r.status_code == 200 and "привет" in r.text and "message_stop" in r.text
        assert seen[0][0] == "https://agg.example/v1/messages?beta=true" and seen[-1][2] == "claude-code-20250219"
        assert {k for _, k, _, _ in seen} >= {"2222"}
        keys = {k["tail"]: k for k in api.get("/api/keys").json()["keys"]}
        assert keys["1111"]["status"] == "limited"  # left the pool; re-checked in the background
        assert keys["2222"]["requests"] == 2 and keys["2222"]["tokens_in"] == 400 and keys["2222"]["tokens_out"] == 80
        assert access.tokens == [400, 80]
        assert api.get(f"/api/llm-proxy/{token}/v1/models").json()["data"][0]["id"] == "cc/claude-opus-5-5"
    finally:
        proxy.close_access(token)
    assert api.post(f"/api/llm-proxy/{token}/v1/messages", json={}).status_code == 401  # gone with its run
    assert api.post("/api/llm-proxy/made-up/v1/messages", json={}).status_code == 401


def test_proxy_errors_are_anthropic_shaped(api, monkeypatch):
    _add_key(api, "sk-third-3333")
    monkeypatch.setattr(proxy, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad tool"}})))
    me = api.get("/api/auth/me").json()
    token = proxy.open_access(proxy.RunAccess(user_id=me["id"], run_id=2, models={}, default_provider="custom"))
    try:
        r = api.post(f"/api/llm-proxy/{token}/v1/messages", json={"model": "x", "messages": []})
        assert r.status_code == 400 and r.json()["error"]["message"] == "bad tool"
        keys = api.get("/api/keys").json()["keys"]
        assert keys[0]["status"] == "available"  # a bad request is not the key's fault
    finally:
        proxy.close_access(token)


def test_anthropic_base_addresses():
    assert proxy.anthropic_base("custom", "https://anymodel.org/v1") == "https://anymodel.org"
    assert proxy.anthropic_base("custom", "https://x.example/api/") == "https://x.example/api"
    assert proxy.anthropic_base("anthropic", None) == "https://api.anthropic.com"
    assert proxy.anthropic_base("groq", None) is None


# ------------------------------------------------------------------ stream-json -> chat events

def test_claude_code_events_become_chat_events():
    out = []

    async def emit(agent, e):
        out.append((agent, e))

    p = claude_code._Parse(emit, "Orchestrator", "cc/claude-opus-5-5")
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {"type": "system", "subtype": "status", "status": "requesting"},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "thinking", "thinking": "…"},
            {"type": "text", "text": "План готов"},
            {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": [
                {"content": "Сверстать", "status": "in_progress", "activeForm": "Верстаю"}]}},
            {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"subagent_type": "coder", "prompt": "…"}}]}},
        {"type": "system", "subtype": "task_started", "tool_use_id": "a1", "subagent_type": "coder", "description": "вёрстка"},
        {"type": "assistant", "parent_tool_use_id": "a1", "message": {"content": [
            {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "p/index.html", "content": "<p>\n</p>\n"}}]}},
        {"type": "user", "parent_tool_use_id": "a1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "w1", "content": "File created"}]}},
        {"type": "system", "subtype": "task_notification", "tool_use_id": "a1", "status": "completed", "summary": "готово"},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "mcp__aidd__Screenshot", "input": {"path": "p/index.html"}}]}},
        {"type": "user", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s1", "is_error": True, "content": [{"type": "text", "text": "no browser"}]}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Готово", "session_id": "s-1",
         "modelUsage": {"cc/claude-opus-5-5": {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 50},
                        "mini": {"inputTokens": 10, "outputTokens": 5}}},
    ]

    async def go():
        for ev in events:
            await p.feed(ev)

    asyncio.run(go())
    kinds = [(a, e["type"]) for a, e in out]
    assert kinds == [("lead", "waiting"), ("lead", "text"), ("lead", "todos"), ("lead.1", "agent_start"),
                     ("lead.1", "tool"), ("lead.1", "tool_done"), ("lead.1", "agent_done"), ("lead", "tool"),
                     ("lead", "tool_done")]
    by = {(a, e["type"]): e for a, e in out}
    assert by[("lead.1", "agent_start")]["role"] == "Coder" and by[("lead.1", "agent_start")]["description"] == "вёрстка"
    assert by[("lead.1", "tool")]["arg"] == "p/index.html" and by[("lead.1", "tool_done")]["ok"]
    assert by[("lead", "todos")]["todos"] == [{"content": "Сверстать", "status": "in_progress"}]
    assert by[("lead", "tool")]["tool"] == "Screenshot" and by[("lead", "tool_done")]["note"] == "no browser"
    assert p.result == "Готово" and p.session_id == "s-1" and p.error is None and p.tokens == [160, 25]


def test_a_failed_result_is_an_error():
    async def emit(*a):
        pass

    p = claude_code._Parse(emit, "Orchestrator", "m")
    asyncio.run(p.feed({"type": "result", "is_error": True, "result": "API Error: 401 invalid key"}))
    assert p.error == "API Error: 401 invalid key"


# ------------------------------------------------------------------ sandbox, skills, agents, choice

def test_the_sandbox_hides_the_server_and_runs_the_real_binary(tmp_path, monkeypatch):
    from app.config import DATA_DIR, get_settings

    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "auto")
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: "/usr/bin/bwrap")
    ws = DATA_DIR / "workspaces" / "chat-test"
    argv = claude_code._sandbox(["/usr/local/bin/claude", "-p", "x"], ws, "/usr/local/bin/claude", [])
    assert argv[0] == "bwrap" and ["--tmpfs", str(DATA_DIR)] == argv[argv.index("--tmpfs", 9):argv.index("--tmpfs", 9) + 2] \
        or ("--tmpfs" in argv and str(DATA_DIR) in argv)
    i = argv.index("--bind")
    assert argv[i + 1] == str(ws.resolve())
    assert argv[-3:] == [str(Path("/usr/local/bin/claude").resolve()), "-p", "x"]
    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "off")
    assert claude_code._sandbox(["claude", "-p"], ws, "claude", []) == ["claude", "-p"]


def test_skills_are_linked_where_claude_code_looks(tmp_path, monkeypatch):
    from app.agents import skills as skills_mod

    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    skills_mod.install_text(7, "---\nname: brand\ndescription: Our voice.\n---\nBe brief.")
    home = tmp_path / "ws" / ".home"
    roots = claude_code._link_skills(home, 7, None)
    linked = {p.name for p in (home / ".claude" / "skills").iterdir()}
    assert {"brand", "html-prototype", "docx"} <= linked and (home / ".claude/skills/brand/SKILL.md").read_text().endswith("brief.")
    claude_code._link_skills(home, 7, ["brand"])  # a role limited to some skills
    assert {p.name for p in (home / ".claude" / "skills").iterdir()} == {"brand"}
    assert any(str(r).endswith("brand") for r in roots)


def test_roles_become_claude_code_agents_on_their_models():
    specs = {"Orchestrator": AgentSpec(role="Orchestrator", provider="custom", model="opus"),
             "Coder": AgentSpec(role="Coder", provider="custom", model="coder-model", instructions="Use TypeScript."),
             "Tester": AgentSpec(role="Tester", provider="custom", model="mini")}
    agents = claude_code._agents(specs, "Orchestrator")
    assert set(agents) == {"coder", "tester"}
    assert agents["coder"]["model"] == "coder-model" and "Use TypeScript." in agents["coder"]["prompt"]


def test_which_engine_runs(monkeypatch):
    from app.config import get_settings

    async def installed(refresh=False):
        return {"installed": True, "version": "2.1"}

    monkeypatch.setattr(claude_code, "status", installed)
    custom = AgentSpec(role="Orchestrator", provider="custom", model="m")
    groq = AgentSpec(role="Orchestrator", provider="groq", model="m")
    monkeypatch.setattr(get_settings(), "ENGINE", "auto")
    run = lambda *a: asyncio.run(claude_code.should_use(*a))  # noqa: E731
    assert run(None, custom) and not run(None, groq)  # auto: only an Anthropic-format aggregator
    assert run("claude_code", groq) and not run("native", custom)

    async def missing(refresh=False):
        return {"installed": False}

    monkeypatch.setattr(claude_code, "status", missing)
    assert not run("claude_code", custom)


def test_engine_setting_api(api, monkeypatch):
    async def installed(refresh=False):
        return {"installed": True, "version": "2.1.332"}

    monkeypatch.setattr(claude_code, "status", installed)
    got = api.get("/api/engine").json()
    assert got["installed"] and got["version"] == "2.1.332" and got["engine"] == "auto"
    assert api.put("/api/engine", json={"engine": "claude_code"}).json()["engine"] == "claude_code"
    assert api.put("/api/engine", json={"engine": "bogus"}).status_code == 422
    assert api.put("/api/engine", json={"engine": "auto"}).json()["engine"] == "auto"


# ------------------------------------------------------------------ the command line of a turn

class _FakeProc:
    def __init__(self, lines):
        self.pid = 999999
        self.returncode = None
        self._lines = [json.dumps(x).encode() + b"\n" for x in lines]

        async def gen(items):
            for i in items:
                yield i

        self.stdout = gen(self._lines)
        self.stderr = gen([])

    async def wait(self):
        self.returncode = 0
        return 0


def test_a_turn_passes_effort_and_turns_off_web_search_for_aggregators(tmp_path, monkeypatch):
    from app.agents.department import RunState

    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"], seen["env"] = list(argv), kw["env"]
        return _FakeProc([{"type": "system", "subtype": "init", "session_id": "s-9"},
                          {"type": "result", "is_error": False, "result": "ok", "modelUsage": {}}])

    monkeypatch.setattr(claude_code, "find_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(claude_code, "_sandbox", lambda argv, *a: argv)
    events = []

    class St(RunState):
        async def event(self, agent_id, data):
            events.append((agent_id, data["type"]))

        async def alive(self):
            return True

    ws = tmp_path / "ws"
    ws.mkdir()
    lead = AgentSpec(role="Orchestrator", provider="custom", model="cc/claude-opus-5-5", effort="max",
                     fallbacks=[{"provider": "custom", "model": "cc/claude-sonnet"}])
    st = St(1, 1, ws, {"Orchestrator": lead, "Coder": AgentSpec(role="Coder", provider="custom", model="coder-m")},
            asyncio.Semaphore(1))
    text = asyncio.run(claude_code.run_turn(st, "Orchestrator", lead, "Сделай", mode="department"))
    argv, env = seen["argv"], seen["env"]
    assert text == "ok"
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("--autocompact") + 1] == "160000"  # not ~900k for a model name it does not know
    assert argv[argv.index("--disallowedTools") + 1] == "WebSearch"
    assert argv[argv.index("--fallback-model") + 1] == "cc/claude-sonnet"
    assert json.loads(argv[argv.index("--agents") + 1])["coder"]["model"] == "coder-m"
    assert argv[argv.index("--model") + 1] == "cc/claude-opus-5-5" and argv[-1] == "Сделай"
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:8000/api/llm-proxy/")
    assert "sk-" not in json.dumps(env)  # no pool key reaches Claude Code
    assert env["HOME"] == str(ws / ".home") and env["ENABLE_TOOL_SEARCH"] == "false"
    assert ("lead", "agent_done") in events
    direct = AgentSpec(role="Orchestrator", provider="anthropic", model="claude-opus-5-5")
    asyncio.run(claude_code.run_turn(st, "Orchestrator", direct, "x", mode="single"))
    assert "--disallowedTools" not in seen["argv"] and "--effort" not in seen["argv"]  # Anthropic runs web search


def test_effort_per_message(api, monkeypatch):
    from app.agents import department as dept

    monkeypatch.setattr(dept, "start", lambda run_id: None)  # no real turn
    _add_key(api, "sk-effort-4444")
    r = api.post("/api/chats", json={"prompt": "hi", "mode": "single", "effort": "xhigh"})
    assert r.status_code in (200, 201), r.text
    chat = r.json()
    assert api.post("/api/chats", json={"prompt": "x", "effort": "huge"}).status_code == 400
    from app.agents.department import _lead_spec

    spec = AgentSpec(role="Orchestrator", provider="custom", model="m", effort="low")
    assert _lead_spec({"Orchestrator": spec}, "Orchestrator", "department", None, "max")[1].effort == "max"
    assert _lead_spec({"Orchestrator": spec}, "Orchestrator", "department", None, None)[1].effort == "low"

    async def stored():
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import AgentRun

        async with SessionLocal() as db:
            return [r.effort for r in (await db.execute(select(AgentRun).where(AgentRun.chat_id == chat["id"]))).scalars()]

    assert asyncio.run(stored()) == ["xhigh"]
