"""The common key pool and the model-call layer, against fake providers (no network).

A fake OpenAI-compatible aggregator answers per key: ok / 429 (+Retry-After) / 402 /
401 / 500, and can return tool calls. The Anthropic client is checked through the
official SDK with a mocked HTTP transport."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import httpx
import pytest

from app.agents import llm
from app.agents.keypool import KeyPool
from app.agents.llm import Request
from app.agents.providers import get_provider


class FakeAggregator:
    """state per secret: "ok" | "429" | "402" | "401" | "500" | "500once"."""

    def __init__(self, states: dict[str, str]):
        self.states = states
        self.calls: list[tuple[str, str, dict]] = []  # (secret, path, body)
        self.reply = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 3}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        secret = request.headers["authorization"].split(" ", 1)[1]
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        self.calls.append((secret, request.url.path, body))
        state = self.states.get(secret, "401")
        if state == "500once":
            self.states[secret] = "ok"
            return httpx.Response(500, json={"error": {"message": "upstream"}}, headers={"retry-after": "0.01"})
        if state == "429":
            return httpx.Response(429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "1"})
        if state == "402":
            return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
        if state == "401":
            return httpx.Response(401, json={"error": {"message": "No auth credentials found"}})
        if state == "500":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m-b"}, {"id": "m-a"}]})
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"limit_remaining": None}})
        return httpx.Response(200, json=self.reply)

    def install(self, monkeypatch):
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(llm, "CLIENT_FACTORY",
                            lambda provider, secret, base_url: llm.OpenAICompatible(provider, secret, base_url,
                                                                                    transport=transport))


async def _pool_with_keys(secrets: list[str], provider: str = "openrouter"):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import ApiKey
    from app.security import encrypt_str

    path = os.path.join(tempfile.mkdtemp(prefix="aidd-pool-"), "pool.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        for i, secret in enumerate(secrets):
            db.add(ApiKey(user_id=1, provider=provider, label=f"k{i + 1}", key_encrypted=encrypt_str(secret),
                          key_tail=secret[-4:], status="available"))
        await db.commit()
    pool = KeyPool(session_factory=sessions)
    events: list[dict] = []

    async def on_event(user_id, event):
        events.append(event)

    pool.on_event = on_event
    return pool, sessions, events, engine


async def _statuses(sessions) -> dict[str, str]:
    from sqlalchemy import select

    from app.models import ApiKey

    async with sessions() as db:
        return {k.label: k.status for k in (await db.execute(select(ApiKey))).scalars()}


REQ = Request(model="m-a", messages=[{"role": "user", "content": "hello"}])


def test_error_sorting():
    c = llm.classify
    assert c(429, "rate limited", {"retry-after": "7"}) == (llm.RATE_LIMITED, 7.0)
    assert c(429, "You exceeded your current quota", {}) == (llm.NO_CREDITS, None)
    assert c(402, "x") == (llm.NO_CREDITS, None)
    assert c(401, "x") == (llm.INVALID, None)
    assert c(403, "Your input was flagged by moderation", provider_id="openrouter") == (llm.REQUEST, None)
    assert c(400, "Your credit balance is too low") == (llm.NO_CREDITS, None)
    assert c(529, "overloaded")[0] == llm.TRANSIENT
    assert c(404, "no such model") == (llm.REQUEST, None)
    assert c(429, "", {"x-ratelimit-reset-requests": "250ms"}) == (llm.RATE_LIMITED, 0.25)


def test_a_rate_limited_key_hands_the_call_to_another_key(monkeypatch):
    fake = FakeAggregator({"sk-one-1111": "429", "sk-two-2222": "ok", "sk-three-3333": "ok"})
    fake.install(monkeypatch)

    async def go():
        pool, sessions, events, engine = await _pool_with_keys(["sk-one-1111", "sk-two-2222", "sk-three-3333"])
        resp = await pool.complete(1, "openrouter", REQ)
        st = await _statuses(sessions)
        await engine.dispose()
        return resp, st, events

    resp, st, events = asyncio.run(go())
    assert resp.text == "hi" and resp.tokens_in == 10
    assert st == {"k1": "limited", "k2": "available", "k3": "available"}
    kinds = [e["type"] for e in events]
    assert kinds == ["key_limited", "key_rotation"]
    assert events[0]["tail"] == "1111" and "429" in events[0]["reason"]
    assert all("sk-" not in json.dumps(e) for e in events)  # never the secret


def test_calls_spread_over_free_keys(monkeypatch):
    fake = FakeAggregator({"sk-aaaa-1111": "ok", "sk-bbbb-2222": "ok"})
    fake.install(monkeypatch)

    async def go():
        pool, sessions, events, engine = await _pool_with_keys(["sk-aaaa-1111", "sk-bbbb-2222"])
        leases = [await pool.lease(1, "openrouter", "m-a") for _ in range(4)]
        tails = sorted(lease.tail for lease in leases)
        for lease in leases:
            await pool.release(lease, llm.OK)
        await engine.dispose()
        return tails

    assert asyncio.run(go()) == ["1111", "1111", "2222", "2222"]


def test_every_key_limited_waits_and_the_background_check_brings_one_back(monkeypatch):
    fake = FakeAggregator({"sk-one-1111": "429", "sk-two-2222": "402"})
    fake.install(monkeypatch)

    async def go():
        pool, sessions, events, engine = await _pool_with_keys(["sk-one-1111", "sk-two-2222"])
        call = asyncio.create_task(pool.complete(1, "openrouter", REQ))
        await asyncio.sleep(0.5)
        assert not call.done()  # both keys limited: waiting, not failing
        assert await pool.check_due() == 0  # the 429 key is not due before its Retry-After (1 s)
        fake.states["sk-one-1111"] = "ok"  # the limit is over
        await asyncio.sleep(0.8)
        assert await pool.check_due() == 1  # due now: probed with the model it was limited on
        resp = await asyncio.wait_for(call, 5)
        st = await _statuses(sessions)
        await engine.dispose()
        return resp, st, events

    resp, st, events = asyncio.run(go())
    assert resp.text == "hi"
    assert st == {"k1": "available", "k2": "limited"}
    assert [e["type"] for e in events][:4] == ["key_limited", "key_rotation", "key_limited", "keys_waiting"]
    assert any(e["type"] == "key_recovered" and e["tail"] == "1111" for e in events)
    probes = [body for secret, path, body in fake.calls if secret == "sk-one-1111" and body.get("max_tokens") == 1]
    assert probes and probes[-1]["model"] == "m-a"


def test_rejected_keys_leave_the_pool_and_a_pool_without_usable_keys_fails_fast(monkeypatch):
    fake = FakeAggregator({"sk-bad-1111": "401"})
    fake.install(monkeypatch)

    async def go():
        pool, sessions, events, engine = await _pool_with_keys(["sk-bad-1111"])
        with pytest.raises(llm.LLMError) as err:
            await asyncio.wait_for(pool.complete(1, "openrouter", REQ), 5)
        st = await _statuses(sessions)
        await engine.dispose()
        return err.value, st

    err, st = asyncio.run(go())
    assert st == {"k1": "invalid"} and err.outcome == llm.REQUEST and "No usable" in str(err)


def test_a_temporary_failure_is_retried(monkeypatch):
    fake = FakeAggregator({"sk-one-1111": "500once"})
    fake.install(monkeypatch)

    async def go():
        pool, sessions, events, engine = await _pool_with_keys(["sk-one-1111"])
        resp = await pool.complete(1, "openrouter", REQ)
        st = await _statuses(sessions)
        await engine.dispose()
        return resp, st

    resp, st = asyncio.run(go())
    assert resp.text == "hi" and st == {"k1": "available"}


def test_openai_tool_calls_round_trip(monkeypatch):
    fake = FakeAggregator({"sk-one-1111": "ok"})
    fake.reply = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"path": "a.txt"}'}},
        {"id": "c2", "type": "function", "function": {"name": "Bash", "arguments": "{broken"}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    transport = httpx.MockTransport(fake.handler)

    async def go():
        client = llm.OpenAICompatible(get_provider("groq"), "sk-one-1111", transport=transport)
        req = Request(model="m", system="sys", tools=[{"name": "Read", "description": "read", "input_schema": {
            "type": "object", "properties": {"path": {"type": "string"}}}}], messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                              {"type": "tool_use", "id": "t0", "name": "Read", "input": {"path": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "data", "is_error": True}]},
        ])
        resp = await client.create(req)
        await client.close()
        return resp

    resp = asyncio.run(go())
    body = fake.calls[-1][2]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "Read", "arguments": '{"path": "x"}'}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "t0", "content": "ERROR: data"}
    assert body["tools"][0]["function"]["name"] == "Read"
    assert resp.stop == "tool_use"
    assert resp.tool_calls[0] == {"type": "tool_use", "id": "c1", "name": "Read", "input": {"path": "a.txt"}}
    assert "__invalid_json__" in resp.tool_calls[1]["input"]


def test_anthropic_through_the_official_sdk():
    import anthropic
    import httpx2

    seen = []

    def handler(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        sent_key = request.headers.get("x-api-key") or request.headers.get("authorization", "").split(" ")[-1]
        if sent_key == "sk-ant-limited":
            return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
                                   headers={"retry-after": "12"})
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_reason": "tool_use", "stop_sequence": None,
            "content": [{"type": "text", "text": "Reading"},
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "a.txt"}}],
            "usage": {"input_tokens": 20, "output_tokens": 4}})

    async def go():
        http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
        good = llm.AnthropicClient(get_provider("anthropic"), "sk-ant-good", http_client=http)
        resp = await good.create(Request(model="claude-opus-5-5", effort="high", system="sys", messages=[
            {"role": "user", "content": "go"}], tools=[{"name": "Read", "description": "r", "input_schema": {
                "type": "object", "properties": {"path": {"type": "string"}}}}]))
        bad = llm.AnthropicClient(get_provider("anthropic"), "sk-ant-limited",
                                  http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
        with pytest.raises(llm.LLMError) as err:
            await bad.create(Request(model="claude-sonnet-4-6", messages=[{"role": "user", "content": "x"}]))
        return resp, err.value

    resp, err = asyncio.run(go())
    headers, body = seen[0]
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert body["fallbacks"] == "default" and body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"} and body["system"] == "sys"
    assert resp.stop == "tool_use" and resp.tool_calls[0]["input"] == {"path": "a.txt"} and resp.tokens_in == 20
    assert resp.raw[1]["type"] == "tool_use"  # replayed unchanged on the next turn
    assert err.outcome == llm.RATE_LIMITED and err.retry_after == 12.0
    assert "fallbacks" not in seen[1][1]  # only for the models that support it


# ---------------------------------------------------------------- the REST API

@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}).json()[
            "access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        for key in client.get("/api/keys").json()["keys"]:
            client.delete(f"/api/keys/{key['id']}")
        yield client


def test_keys_api(api, monkeypatch):
    fake = FakeAggregator({"sk-or-good-a91f": "ok", "sk-or-slow-07c2": "429", "sk-or-bad-dead": "401"})
    fake.install(monkeypatch)
    providers = api.get("/api/keys/providers").json()
    assert any(p["id"] == "openrouter" for p in providers) and any(p["needs_base_url"] for p in providers)

    r = api.post("/api/keys", json={"provider": "openrouter", "key": "sk-or-good-a91f", "label": "Основной"})
    assert r.status_code == 201, r.text
    good = r.json()
    assert good["tail"] == "a91f" and good["status"] == "available"
    assert api.post("/api/keys", json={"provider": "openrouter", "key": "sk-or-good-a91f"}).status_code == 409
    assert api.post("/api/keys", json={"provider": "openrouter", "key": "sk-or-bad-dead"}).status_code == 400
    slow = api.post("/api/keys", json={"provider": "openrouter", "key": "sk-or-slow-07c2"}).json()
    assert slow["status"] == "limited" and slow["next_check_in"] > 200
    assert api.post("/api/keys", json={"provider": "custom", "key": "sk-custom-1234"}).status_code == 400

    listing = api.get("/api/keys")
    assert "sk-or-" not in listing.text  # the secrets never come back
    assert listing.json()["summary"]["available"] == 1 and listing.json()["summary"]["limited"] == 1

    fake.states["sk-or-slow-07c2"] = "ok"
    checked = api.post(f"/api/keys/{slow['id']}/check").json()
    assert checked["outcome"] == "ok" and checked["key"]["status"] == "available"

    assert api.patch(f"/api/keys/{good['id']}", json={"disabled": True}).json()["status"] == "disabled"
    assert api.get("/api/keys/models", params={"provider": "openrouter"}).json()["models"] == ["m-a", "m-b"]
    assert api.delete(f"/api/keys/{good['id']}").status_code == 204
    assert len(api.get("/api/keys").json()["keys"]) == 1


def test_a_web_page_instead_of_an_api_gives_a_clear_error():
    page = "<!DOCTYPE html><html><head><title>Site</title></head><body>hello</body></html>"

    def handler(request):
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    async def go():
        client = llm.OpenAICompatible(get_provider("custom"), "sk-x", "https://site.example", transport=httpx.MockTransport(handler))
        errors = []
        for call in (lambda: client.create(REQ), lambda: client.probe(None), lambda: client.models_meta()):
            try:
                await call()
            except llm.LLMError as exc:
                errors.append(exc)
        await client.close()
        return errors

    errors = asyncio.run(go())
    assert len(errors) == 3 and all(e.outcome == llm.REQUEST and "<html" not in str(e) for e in errors)
    assert "ends with /v1" in str(errors[0])


def test_adding_a_custom_key_with_the_site_address_finds_the_api(api, monkeypatch):
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "zm-1"}]})
        return httpx.Response(200, text="<!doctype html><html>site</html>", headers={"content-type": "text/html"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm, "CLIENT_FACTORY", lambda provider, secret, base_url: llm.OpenAICompatible(
        provider, secret, base_url, transport=transport))
    r = api.post("/api/keys", json={"provider": "custom", "key": "sk-custom-9999", "base_url": "https://agg.example"})
    assert r.status_code == 201, r.text
    assert r.json()["base_url"] == "https://agg.example/v1"

    def site_only(request):
        return httpx.Response(200, text="<!doctype html><html>site</html>", headers={"content-type": "text/html"})

    transport2 = httpx.MockTransport(site_only)
    monkeypatch.setattr(llm, "CLIENT_FACTORY", lambda provider, secret, base_url: llm.OpenAICompatible(
        provider, secret, base_url, transport=transport2))
    bad = api.post("/api/keys", json={"provider": "custom", "key": "sk-custom-8888", "base_url": "https://only-site.example"})
    assert bad.status_code == 400 and "/v1" in bad.json()["detail"]
    models = api.get("/api/keys/models/all").json()["providers"]
    assert any(p["provider"] == "custom" for p in models)
