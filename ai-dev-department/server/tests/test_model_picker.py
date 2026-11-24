"""Automatic model choice: strength scores, real checks, role assignment, fallbacks."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

from app.agents import department, llm, model_picker, skills as skills_mod
from app.agents.llm import LLMError, LLMResponse
from app.agents.model_picker import score


def test_strength_scores_order_models_sensibly():
    assert score("anthropic/claude-opus-5") > score("anthropic/claude-sonnet-5") > score("anthropic/claude-haiku-5")
    assert score("openai/gpt-5") > score("openai/gpt-5-mini") > score("openai/gpt-5-nano")
    assert score("meta-llama/llama-3.1-405b-instruct") > score("meta-llama/llama-3.1-70b-instruct") > score(
        "meta-llama/llama-3.1-8b-instruct")
    assert score("google/gemini-3-pro") > score("google/gemini-3-flash") > score("google/gemini-3-flash-lite")
    assert score("deepseek/deepseek-r1") > score("deepseek/deepseek-chat")
    # the price per output token beats the name when the service tells it
    assert score("vendor/unknown-x", {"price_out": 30.0}) > score("vendor/unknown-y-pro", {"price_out": 0.4})
    assert score("x/model:free") < score("x/model")
    for not_a_chat in ("text-embedding-3-large", "whisper-large-v3", "llama-guard-4-12b", "dall-e-3", "tts-1"):
        assert score(not_a_chat) is None, not_a_chat
    assert score("vendor/m", {"tools": False}) is None  # agents need tool calls
    assert score("vendor/m", {"context": 4096}) is None


OR_MODELS = {"data": [
    {"id": "acme/titan-pro", "pricing": {"completion": "0.00006"}, "context_length": 400000,
     "supported_parameters": ["tools", "temperature"]},
    {"id": "acme/titan-mini", "pricing": {"completion": "0.000002"}, "context_length": 128000,
     "supported_parameters": ["tools"]},
    {"id": "acme/no-tools-ultra", "pricing": {"completion": "0.0001"}, "supported_parameters": ["temperature"]},
    {"id": "acme/broken-large", "pricing": {"completion": "0.00003"}, "supported_parameters": ["tools"]},
    {"id": "acme/chatty-plus", "pricing": {"completion": "0.00002"}, "supported_parameters": ["tools"]},
    {"id": "open/llama-70b", "pricing": {"completion": "0.0000008"}, "supported_parameters": ["tools"]},
    {"id": "acme/text-embedding-big", "pricing": {"completion": "0"}},
]}
GROQ_MODELS = {"data": [{"id": "llama-70b"}, {"id": "tiny-8b"}]}


class FakeProviderClient:
    """openrouter-like (prices, tool support) and groq-like (ids only) catalogues; the
    model decides how it answers the tool check."""

    def __init__(self, provider, secret, base_url):
        self.provider = provider

    async def close(self):
        pass

    async def probe(self, model):
        return None

    async def models_meta(self):
        transport = None  # parse with the real code
        raw = OR_MODELS if self.provider.id == "openrouter" else GROQ_MODELS
        import httpx

        client = llm.OpenAICompatible(self.provider, "k", transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=raw)))
        try:
            return await client.models_meta()
        finally:
            await client.close()

    async def create(self, req):
        if "broken" in req.model:
            raise LLMError(llm.REQUEST, "No endpoints found for this model", status=404)
        if "chatty" in req.model:
            return LLMResponse([{"type": "text", "text": "Sure! I am ready."}], "end", 5, 5)
        return LLMResponse([{"type": "tool_use", "id": "t", "name": "report_ready", "input": {"ready": True}}],
                           "tool_use", 5, 5)


@pytest.fixture()
def pool(monkeypatch):
    from tests.test_keypool import _pool_with_keys

    async def make():
        return await _pool_with_keys([])

    pool_, sessions, events, engine = asyncio.run(make())
    monkeypatch.setattr(model_picker, "key_pool", pool_)
    monkeypatch.setattr(llm, "CLIENT_FACTORY", FakeProviderClient)
    return pool_, sessions, engine


async def _add_keys(sessions, *providers):
    from app.models import ApiKey
    from app.security import encrypt_str

    async with sessions() as db:
        for i, p in enumerate(providers):
            db.add(ApiKey(user_id=1, provider=p, label=p, key_encrypted=encrypt_str(f"sk-{p}-{i}000"),
                          key_tail=f"{i}000", status="available"))
        await db.commit()


def test_pick_checks_the_best_models_and_assigns_roles_by_strength(pool):
    pool_, sessions, engine = pool

    async def go():
        await _add_keys(sessions, "openrouter", "groq")
        async with sessions() as db:
            result = await model_picker.pick(db, 1)
            await model_picker.apply(db, 1, result["assignments"])
        from sqlalchemy import select

        from app.models import AgentRole

        async with sessions() as db:
            roles = {r.role: r for r in (await db.execute(select(AgentRole))).scalars()}
        await engine.dispose()
        return result, roles

    result, roles = asyncio.run(go())
    ranking = {r["model"]: r for r in result["ranking"]}
    assert "acme/no-tools-ultra" not in ranking and "acme/text-embedding-big" not in ranking
    assert ranking["acme/broken-large"]["works"] is False and "404" in ranking["acme/broken-large"]["check"]
    assert ranking["acme/chatty-plus"]["works"] is False and "tools" in ranking["acme/chatty-plus"]["check"]
    by_role = {a["role"]: a for a in result["assignments"]}
    assert by_role["Orchestrator"]["model"] == "acme/titan-pro"  # the strongest working model leads
    order = [by_role[r]["model"] for r in model_picker.ROLE_ORDER]
    scores = [by_role[r]["score"] for r in model_picker.ROLE_ORDER]
    assert scores == sorted(scores, reverse=True)  # the other roles in descending strength
    assert set(order) == {"acme/titan-pro", "acme/titan-mini", "open/llama-70b", "llama-70b", "tiny-8b"} or \
        len(set(order)) >= 3
    # «llama-70b» is offered by two providers: one entry, the other offer is its first fallback
    llama_role = next(a for a in result["assignments"] if model_picker.base_name(a["model"]) == "llama-70b")
    assert {(f["provider"], model_picker.base_name(f["model"])) for f in llama_role["fallbacks"]} & {
        ("groq", "llama-70b"), ("openrouter", "llama-70b")}
    assert roles["Orchestrator"].auto and roles["Orchestrator"].fallbacks  # saved with fallbacks


# ---------------------------------------------------------------- in a run

def _call(tool, n=0, **args):
    return {"type": "tool_use", "id": f"{tool}-{n}", "name": tool, "input": args}


class RunClient:
    """Two providers: «groq» keys are all rate-limited, «openrouter» works. Probes pass."""

    calls: list[tuple[str, str]] = []

    def __init__(self, provider, secret, base_url):
        self.provider, self.secret = provider, secret

    async def close(self):
        pass

    async def probe(self, model):
        return None

    async def models_meta(self):
        if self.provider.id == "groq":
            return [{"id": "big-model-405b", "price_out": 40.0, "context": None, "tools": None, "kind": ""}]
        return [{"id": "acme/solid-pro", "price_out": 10.0, "context": 200000, "tools": True, "kind": ""}]

    async def create(self, req):
        RunClient.calls.append((self.provider.id, req.model))
        if not req.system:  # the model check
            return LLMResponse([_call("report_ready", ready=True)], "tool_use", 1, 1)
        if self.provider.id == "groq" and RunClient.limit_groq:
            raise LLMError(llm.RATE_LIMITED, "slow", status=429, retry_after=60)
        role = re.search(r"You are the (\w+)", req.system).group(1)
        if role == "Orchestrator" and sum(1 for m in req.messages if m["role"] == "assistant") == 0:
            return LLMResponse([_call("Task", role="Coder", prompt="write x.txt")], "tool_use", 10, 5)
        if role == "Coder" and sum(1 for m in req.messages if m["role"] == "assistant") == 0:
            return LLMResponse([_call("Write", path="x.txt", content="x")], "tool_use", 10, 5)
        return LLMResponse([{"type": "text", "text": f"{role} done"}], "end", 10, 5)

    limit_groq = False


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "off")
    monkeypatch.setattr(get_settings(), "AGENT_FALLBACK_AFTER_SECONDS", 1)
    monkeypatch.setattr(department, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm, "CLIENT_FACTORY", RunClient)
    RunClient.calls, RunClient.limit_groq = [], False
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}).json()[
            "access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        for key in client.get("/api/keys").json()["keys"]:
            client.delete(f"/api/keys/{key['id']}")
        for role in client.get("/api/agent-roles").json():
            client.delete(f"/api/agent-roles/{role['role']}")
        yield client


def _wait_run(api, run_id, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = api.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("done", "failed", "stopped") and not run["live"]:
            return run
        time.sleep(0.2)
    raise AssertionError(run)


def test_a_first_run_chooses_the_models_itself_and_switches_model_when_a_provider_is_out(api):
    assert api.post("/api/runs", json={"prompt": "do it"}).status_code == 400  # no keys at all
    assert api.post("/api/keys", json={"provider": "groq", "key": "sk-groq-1111"}).status_code == 201
    assert api.post("/api/keys", json={"provider": "openrouter", "key": "sk-or-2222"}).status_code == 201

    # nothing set up: the run picks the models (the strongest leads), then works
    run_id = api.post("/api/runs", json={"prompt": "make x.txt"}).json()["id"]
    run = _wait_run(api, run_id)
    assert run["status"] == "done", run
    events = api.get(f"/api/runs/{run_id}/events").json()
    auto = [e for e in events if e["type"] == "auto_models"]
    assert [e["data"]["status"] for e in auto] == ["started", "done"]
    roles = {r["role"]: r for r in api.get("/api/agent-roles").json()}
    assert roles["Orchestrator"]["auto"] and roles["Orchestrator"]["model"] == "big-model-405b"
    assert roles["Orchestrator"]["fallbacks"][0]["model"] == "acme/solid-pro"

    # every groq key is now rate-limited: the agents move to the fallback model
    RunClient.limit_groq = True
    run_id = api.post("/api/runs", json={"prompt": "again"}).json()["id"]
    run = _wait_run(api, run_id, seconds=60)
    assert run["status"] == "done", run
    events = api.get(f"/api/runs/{run_id}/events").json()
    switch = [e for e in events if e["type"] == "model_switch"]
    assert switch and switch[0]["data"]["to"] == "openrouter:acme/solid-pro"
    assert ("openrouter", "acme/solid-pro") in RunClient.calls


def test_auto_roles_endpoint(api):
    api.post("/api/keys", json={"provider": "openrouter", "key": "sk-or-2222"})
    job = api.post("/api/agent-roles/auto", json={"check": True}).json()
    deadline = time.monotonic() + 20
    while job["running"] and time.monotonic() < deadline:
        time.sleep(0.2)
        job = api.get("/api/agent-roles/auto").json()
    assert job["error"] is None and job["result"]["assignments"][0] == {
        "role": "Orchestrator", "provider": "openrouter", "model": "acme/solid-pro",
        "score": job["result"]["assignments"][0]["score"], "fallbacks": []}
    roles = {r["role"]: r for r in api.get("/api/agent-roles").json()}
    assert all(roles[r]["ready"] and roles[r]["auto"] for r in model_picker.ROLE_ORDER)
    # choosing another model by hand turns «auto» off for that role
    api.put("/api/agent-roles/Coder", json={"provider": "openrouter", "model": "other"})
    assert {r["role"]: r for r in api.get("/api/agent-roles").json()}["Coder"]["auto"] is False
    assert json.dumps(job).count("sk-") == 0
