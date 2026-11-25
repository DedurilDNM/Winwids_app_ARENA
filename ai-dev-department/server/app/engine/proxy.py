"""The model proxy for the Claude Code engine.

Claude Code talks the Anthropic Messages API to ANTHROPIC_BASE_URL. That URL is this
server, with a per-run token in the path: /api/llm-proxy/<token>. For every request the
proxy finds the provider of the requested model, takes the freest key of the common pool
(«котёл»), and streams the provider's answer back unchanged. A key that hits a limit,
runs out of credits or is rejected leaves the pool and the request goes on with another
key - before a byte of the answer has been sent, so Claude Code never notices.

The token is random, lives only while its run does, and is the only way in: no user
session, no key is ever given to Claude Code.

Two more callbacks for the engine's MCP tools live under the same token:
POST /aidd/sendfile and /aidd/screenshot (see app/engine/mcp_server.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.agents import llm
from app.agents.keypool import key_pool
from app.agents.llm import KEY_PROBLEMS, OK, RATE_LIMITED, TRANSIENT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/llm-proxy", tags=["engine"], include_in_schema=False)

HTTP_TRANSPORT = None  # tests: an httpx transport standing in for the providers
# providers that speak the Anthropic Messages API (base URL without /v1/messages)
ANTHROPIC_BASES = {"anthropic": "https://api.anthropic.com", "deepseek": "https://api.deepseek.com/anthropic"}
# request headers passed on to the provider (the key is put in by the proxy)
PASS_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept", "x-stainless-helper-method",
                "user-agent")  # Claude Code's own: some aggregators turn away unknown clients


def anthropic_base(provider: str, base_url: str | None) -> str | None:
    """Where a key's Anthropic-format API lives, or None if the provider has none we know."""
    if provider in ANTHROPIC_BASES:
        return ANTHROPIC_BASES[provider]
    if base_url:  # «custom» and other OpenAI-compatible aggregators: many serve /v1/messages too
        base = base_url.rstrip("/")
        return base[:-3] if base.endswith("/v1") else base
    return None


@dataclass
class RunAccess:
    user_id: int
    run_id: int
    models: dict[str, str]  # model name -> provider
    default_provider: str
    workspace: Any = None
    emit: Callable[[str, dict], Awaitable[None]] | None = None  # (agent_id, event)
    should_continue: Callable[[], Awaitable[bool]] | None = None
    tokens: list[int] = field(default_factory=lambda: [0, 0])
    requests: int = 0
    created: float = field(default_factory=time.monotonic)


_RUNS: dict[str, RunAccess] = {}


def open_access(access: RunAccess) -> str:
    token = secrets.token_urlsafe(32)
    _RUNS[token] = access
    return token


def close_access(token: str) -> None:
    _RUNS.pop(token, None)


def _access(token: str) -> RunAccess:
    access = _RUNS.get(token)
    if access is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or finished run")
    return access


def _anthropic_error(code: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=code)


def _usage_from_sse(chunk: bytes, tokens: list[int], tail: bytearray) -> None:
    """Count tokens from message_start / message_delta events as they stream past."""
    tail.extend(chunk)
    while b"\n" in tail:
        line, _, rest = bytes(tail).partition(b"\n")
        tail[:] = rest
        if not line.startswith(b"data:"):
            continue
        try:
            data = json.loads(line[5:].strip() or b"{}")
        except ValueError:
            continue
        if data.get("type") == "message_start":
            usage = (data.get("message") or {}).get("usage") or {}
            tokens[0] += int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) \
                + int(usage.get("cache_creation_input_tokens") or 0)
            if len(tokens) > 2:  # how much of the input came from the provider's cache
                tokens[2] += int(usage.get("cache_read_input_tokens") or 0)
        elif data.get("type") == "message_delta":
            tokens[1] = int((data.get("usage") or {}).get("output_tokens") or 0)  # running total of the message


@router.get("/{token}/v1/models")
async def models(token: str) -> dict:
    access = _access(token)
    return {"data": [{"id": m, "type": "model", "display_name": m} for m in access.models], "has_more": False}


@router.post("/{token}/v1/messages/count_tokens")
async def count_tokens(token: str, request: Request) -> Response:
    return await _forward(token, request, "/v1/messages/count_tokens")


@router.post("/{token}/v1/messages")
async def messages(token: str, request: Request) -> Response:
    return await _forward(token, request, "/v1/messages")


async def _forward(token: str, request: Request, path: str) -> Response:
    access = _access(token)
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _anthropic_error(400, "invalid_request_error", "the body is not JSON")
    model = str(body.get("model") or "")
    provider = access.models.get(model) or access.default_provider
    headers = {k: v for k, v in request.headers.items() if k.lower() in PASS_HEADERS}
    query = request.url.query
    url_tail = path + (f"?{query}" if query else "")
    transient = 0
    started = time.monotonic()
    while True:
        try:
            lease = await key_pool.lease(access.user_id, provider, model, should_continue=access.should_continue)
        except llm.LLMError as exc:
            kind = "rate_limit_error" if exc.outcome == RATE_LIMITED else "authentication_error"
            return _anthropic_error(429 if exc.outcome == RATE_LIMITED else 401, kind, str(exc))
        except asyncio.CancelledError:
            return _anthropic_error(499, "api_error", "the run was stopped")
        base = anthropic_base(lease.provider.id, lease.base_url)
        if base is None:
            await key_pool.release(lease, OK)
            return _anthropic_error(400, "invalid_request_error",
                                    f"{lease.provider.name} has no Anthropic-format API: give this role a model of "
                                    "another aggregator, or use the built-in engine")
        out_headers = {**headers, "x-api-key": lease.secret, "authorization": f"Bearer {lease.secret}"}
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=600, write=60, pool=30),
                                   transport=HTTP_TRANSPORT)
        try:
            upstream = await client.send(client.build_request("POST", base + url_tail, content=raw,
                                                              headers=out_headers), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            await key_pool.release(lease, TRANSIENT)
            transient += 1
            if transient <= 3:
                await asyncio.sleep(min(10.0, 2.0 * transient))
                continue
            return _anthropic_error(529, "overloaded_error", f"the provider did not answer ({type(exc).__name__})")
        if upstream.status_code >= 400:
            data = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            message = llm._error_message(upstream)
            outcome, retry = llm.classify(upstream.status_code, message, upstream.headers, lease.provider.id)
            await key_pool.release(lease, outcome, status=upstream.status_code, retry_after=retry)
            if outcome in KEY_PROBLEMS:
                logger.info("Engine proxy: %s key …%s -> %s; trying another key", provider, lease.tail, outcome)
                continue  # another key of the pool (or wait for one)
            if outcome == TRANSIENT and transient < 3:
                transient += 1
                await asyncio.sleep(min(10.0, retry or 2.0 * transient))
                continue
            kind = upstream.headers.get("content-type", "")
            if "json" in kind:
                return Response(data, status_code=upstream.status_code, media_type="application/json")
            return _anthropic_error(upstream.status_code, "api_error", message)
        access.requests += 1
        if path.endswith("count_tokens") or "event-stream" not in upstream.headers.get("content-type", ""):
            data = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            try:
                usage = json.loads(data).get("usage") or {}
                access.tokens[0] += int(usage.get("input_tokens") or 0)
                access.tokens[1] += int(usage.get("output_tokens") or 0)
            except Exception:
                usage = {}
            await key_pool.release(lease, OK, tokens=(int(usage.get("input_tokens") or 0),
                                                      int(usage.get("output_tokens") or 0)))
            return Response(data, status_code=upstream.status_code,
                            media_type=upstream.headers.get("content-type", "application/json"))
        return StreamingResponse(_relay(upstream, client, lease, access, model, started), status_code=upstream.status_code,
                                 media_type="text/event-stream", headers={"cache-control": "no-cache"})


async def _relay(upstream: httpx.Response, client: httpx.AsyncClient, lease, access: RunAccess,
                 model: str = "", started: float | None = None):
    tokens = [0, 0, 0]  # input (all), output, input read from the cache
    tail = bytearray()
    outcome = OK
    first = None
    try:
        async for chunk in upstream.aiter_bytes():  # decoded: the client gets no content-encoding
            if first is None:
                first = time.monotonic()
            _usage_from_sse(chunk, tokens, tail)
            yield chunk
    except (httpx.HTTPError, asyncio.CancelledError):
        outcome = TRANSIENT  # the stream broke off: Claude Code retries the request itself
        raise
    finally:
        await upstream.aclose()
        await client.aclose()
        access.tokens[0] += tokens[0]
        access.tokens[1] += tokens[1]
        await key_pool.release(lease, outcome, tokens=(tokens[0], tokens[1]))
        if started is not None:  # where the time goes: waiting for the first byte vs. writing the answer
            end = time.monotonic()
            wait = (first or end) - started
            write = end - (first or end)
            logger.info("Engine call: run=%s model=%s in=%d cache_read=%d out=%d first_byte=%.1fs answer=%.1fs "
                        "speed=%.0f tok/s %s", access.run_id, model, tokens[0], tokens[2], tokens[1], wait, write,
                        tokens[1] / write if write > 0.5 else 0, "" if outcome == OK else "(broken off)")


# ------------------------------------------------------------------ MCP tool callbacks

@router.post("/{token}/aidd/sendfile")
async def sendfile(token: str, request: Request) -> dict:
    access = _access(token)
    args = await request.json()
    from app.agents.tools import ToolContext, run_tool

    events: list[dict] = []

    async def emit(e):
        events.append(e)

    ctx = ToolContext(workspace=access.workspace, role="Engine", emit=emit, sandbox="off")
    text, is_error = await run_tool(ctx, "SendFile", {"path": args.get("path"), "caption": args.get("caption")})
    if access.emit is not None:
        for e in events:
            await access.emit(str(args.get("agent_id") or "lead"), e)
    return {"text": str(text), "is_error": is_error}


@router.post("/{token}/aidd/screenshot")
async def screenshot(token: str, request: Request) -> dict:
    access = _access(token)
    args = await request.json()
    from app.agents.tools import ToolContext, run_tool

    ctx = ToolContext(workspace=access.workspace, role="Engine", sandbox="off")
    text, is_error = await run_tool(ctx, "Screenshot", {k: args[k] for k in ("path", "viewport", "full_page") if k in args})
    images = [{"mime": kind, "data": data} for kind, data in getattr(text, "images", [])]
    return {"text": str(text), "is_error": is_error, "images": images}
