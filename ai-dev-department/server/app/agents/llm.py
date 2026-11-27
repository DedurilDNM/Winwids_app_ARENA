"""One call to a model, with tools, for any provider in the catalogue.

Neutral message format (Anthropic-like blocks, converted per protocol):
  {"role": "user", "content": "text" | [blocks]}
  {"role": "assistant", "content": [blocks], "_raw": <provider-native content>}
  blocks: {"type": "text", "text"} · {"type": "tool_use", "id", "name", "input"}
          · {"type": "tool_result", "tool_use_id", "content", "is_error"}
Tools: [{"name", "description", "input_schema"}].

Claude models go through the official `anthropic` SDK; every other provider through
the OpenAI-compatible chat-completions protocol (httpx). Errors are sorted into
outcomes the key pool understands: a rate limit or empty credits move the call to
another key; a bad key is put aside; a temporary failure is retried; a bad request
is the caller's problem (no rotation).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agents.providers import Provider, base_url_for

logger = logging.getLogger(__name__)

# outcomes
OK, RATE_LIMITED, NO_CREDITS, INVALID, TRANSIENT, REQUEST = (
    "ok", "rate_limited", "no_credits", "invalid", "transient", "request")
KEY_PROBLEMS = (RATE_LIMITED, NO_CREDITS, INVALID)

# models whose code gets the server-side refusal fallback by default
_FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


class LLMError(Exception):
    def __init__(self, outcome: str, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.status = status
        self.retry_after = retry_after


@dataclass
class LLMResponse:
    content: list[dict]  # neutral blocks
    stop: str  # "end" | "tool_use" | "max_tokens" | "refusal"
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Any = None  # provider-native assistant content (replayed as is on the same provider)
    model: str = ""

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text" and b.get("text")).strip()

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


@dataclass
class Request:
    model: str
    messages: list[dict]
    system: str = ""
    tools: list[dict] = field(default_factory=list)
    max_tokens: int = 16000
    effort: str | None = None  # low | medium | high | xhigh | max (providers that support it)


# ------------------------------------------------------------------ error sorting

_CREDIT_WORDS = ("credit", "balance", "quota", "billing", "payment", "insufficient", "spend limit")


def _retry_after(headers) -> float | None:
    if headers is None:
        return None
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        value = headers.get(name)
        if not value:
            continue
        value = str(value).strip().lower()
        try:
            if value.endswith("ms"):
                return float(value[:-2]) / 1000
            if value.endswith("s") and value[:-1].replace(".", "", 1).isdigit():
                return float(value[:-1])
            if value.endswith("m") and value[:-1].replace(".", "", 1).isdigit():
                return float(value[:-1]) * 60
            return float(value)
        except ValueError:
            continue
    return None


def classify(status: int, message: str = "", headers=None, provider_id: str = "") -> tuple[str, float | None]:
    """HTTP status (+ error text) -> (outcome, retry_after seconds)."""
    text = (message or "").lower()
    if status == 429:
        if any(w in text for w in ("insufficient_quota", "exceeded your current quota", "credits")):
            return NO_CREDITS, None
        return RATE_LIMITED, _retry_after(headers)
    if status == 402:
        return NO_CREDITS, None
    if status == 401:
        return INVALID, None
    if status == 403:
        # OpenRouter answers 403 for a moderated prompt: the key is fine
        if provider_id == "openrouter" and ("moderat" in text or "flagged" in text):
            return REQUEST, None
        return INVALID, None
    if status == 400 and any(w in text for w in _CREDIT_WORDS):
        return NO_CREDITS, None
    if status in (408, 409, 425, 500, 502, 503, 504, 520, 522, 524, 529) or status >= 500:
        return TRANSIENT, _retry_after(headers)
    return REQUEST, None


def short_reason(outcome: str, status: int | None) -> str:
    """A status line for the UI: never the provider's text (may echo the prompt)."""
    names = {RATE_LIMITED: "лимит запросов", NO_CREDITS: "кончились кредиты/квота", INVALID: "ключ не принят",
             TRANSIENT: "временный сбой сервиса", REQUEST: "ошибка запроса"}
    return f"{status} · {names.get(outcome, outcome)}" if status else names.get(outcome, outcome)


# ------------------------------------------------------------------ OpenAI-compatible

def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if m["role"] == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            texts, images = [], []
            for b in content:
                if b.get("type") == "tool_result":
                    body = b.get("content")
                    if isinstance(body, list):  # text and images: tool messages take text only, so the
                        parts = []              # images follow in a user message
                        for part in body:
                            if part.get("type") == "image":
                                src = part.get("source") or {}
                                images.append({"type": "image_url", "image_url": {
                                    "url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
                            elif part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        body = "\n".join(parts)
                    elif not isinstance(body, str):
                        body = json.dumps(body, ensure_ascii=False)
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("ERROR: " if b.get("is_error") else "") + body})
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if images:
                lead = "\n\n".join(texts + ["The images from the tool results above:"])
                out.append({"role": "user", "content": [{"type": "text", "text": lead}] + images})
            elif texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
        else:
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def _openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools]


NOT_AN_API = ("the address answered with a web page, not an API: use the API address from the aggregator's "
              "documentation (it usually ends with /v1), not the address of its site")


def _looks_like_page(resp: httpx.Response) -> bool:
    kind = resp.headers.get("content-type", "")
    return "html" in kind or resp.text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))


def _json(resp: httpx.Response):
    """The JSON body, or a clear error (a web page, an empty answer) - never a raw HTML dump."""
    try:
        return resp.json()
    except ValueError:
        if _looks_like_page(resp):
            raise LLMError(REQUEST, NOT_AN_API, status=resp.status_code) from None
        raise LLMError(TRANSIENT if not resp.content else REQUEST,
                       f"the service did not answer with JSON (HTTP {resp.status_code})", status=resp.status_code) from None


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return NOT_AN_API if _looks_like_page(resp) else resp.text[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)[:300]
    return str(err or data)[:300]


class OpenAICompatible:
    def __init__(self, provider: Provider, secret: str, base_url: str | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 600):
        self.provider = provider
        self.base = base_url_for(provider, base_url)
        if not self.base:
            raise LLMError(REQUEST, "This provider needs a base URL")
        headers = {"Authorization": f"Bearer {secret}"}
        if provider.id == "openrouter":
            headers["X-Title"] = "AIDD"
        self.client = httpx.AsyncClient(base_url=self.base, headers=headers, timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        try:
            resp = await self.client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise LLMError(TRANSIENT, f"timeout: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(TRANSIENT, f"connection: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            message = _error_message(resp)
            outcome, retry = classify(resp.status_code, message, resp.headers, self.provider.id)
            if message == NOT_AN_API and outcome != TRANSIENT:
                outcome = REQUEST  # a wrong address is not a bad key
            raise LLMError(outcome, message, status=resp.status_code, retry_after=retry)
        data = _json(resp)
        # some aggregators answer 200 with an error object (e.g. an upstream limit)
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
            code = int(err.get("code") or 500) if str(err.get("code") or "").isdigit() else 500
            outcome, retry = classify(code, str(err.get("message", "")), resp.headers, self.provider.id)
            raise LLMError(outcome, str(err.get("message", ""))[:300], status=code, retry_after=retry)
        return data

    async def create(self, req: Request) -> LLMResponse:
        body: dict[str, Any] = {"model": req.model, "messages": _to_openai_messages(req.system, req.messages),
                                "max_tokens": req.max_tokens}
        if req.tools:
            body["tools"] = _openai_tools(req.tools)
        if req.effort and self.provider.id in ("openai", "openrouter"):
            body["reasoning_effort" if self.provider.id == "openai" else "reasoning"] = (
                req.effort if self.provider.id == "openai" else {"effort": req.effort})
        data = await self._post("/chat/completions", body)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        blocks: list[dict] = []
        if msg.get("content"):
            text = msg["content"] if isinstance(msg["content"], str) else "".join(
                part.get("text", "") for part in msg["content"] if isinstance(part, dict))
            if text:
                blocks.append({"type": "text", "text": text})
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {"__invalid_json__": str(raw_args)[:2000]}
            blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{len(blocks)}",
                           "name": fn.get("name", ""), "input": args})
        finish = choice.get("finish_reason") or ""
        # «length»: the answer hit the output limit - a tool call in it may be cut off
        stop = ("max_tokens" if finish == "length" else
                "tool_use" if any(b["type"] == "tool_use" for b in blocks) else
                "refusal" if finish == "content_filter" else "end")
        usage = data.get("usage") or {}
        return LLMResponse(blocks, stop, int(usage.get("prompt_tokens") or 0),
                           int(usage.get("completion_tokens") or 0), model=str(data.get("model") or req.model))

    async def probe(self, model: str | None) -> None:
        """Raise LLMError unless the key works (and, with a model, is not rate-limited)."""
        if model:
            await self._post("/chat/completions", {"model": model, "max_tokens": 1,
                                                   "messages": [{"role": "user", "content": "ping"}]})
            return
        path = self.provider.auth_check_path or "/models"
        try:
            resp = await self.client.get(path)
        except httpx.HTTPError as exc:
            raise LLMError(TRANSIENT, f"connection: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            message = _error_message(resp)
            outcome, retry = classify(resp.status_code, message, resp.headers, self.provider.id)
            if message == NOT_AN_API and outcome != TRANSIENT:
                outcome = REQUEST
            raise LLMError(outcome, message, status=resp.status_code, retry_after=retry)
        data = _json(resp)  # a 200 web page is not an API either
        if path == "/models" and not (isinstance(data, list) or (isinstance(data, dict) and "data" in data)):
            raise LLMError(REQUEST, "the address answered, but not with a list of models: is it an OpenAI-compatible API?")
        if self.provider.id == "openrouter" and path == "/key":
            info = (data or {}).get("data") or {}
            remaining = info.get("limit_remaining")
            if remaining is not None and float(remaining) <= 0:
                raise LLMError(NO_CREDITS, "key spending limit reached", status=402)

    async def models_meta(self) -> list[dict]:
        """[{id, price_out (USD per 1M output tokens) | None, context | None, tools: bool | None, kind}]
        from whatever the service tells (OpenRouter: pricing, context, supported parameters;
        Together: type, pricing; Groq: context window; others: ids only)."""
        try:
            # FreeLLMAPI lists its whole catalogue at /models: only the models it can serve now
            resp = await self.client.get("/models?available=true" if self.provider.id == "freellmapi" else "/models")
        except httpx.HTTPError as exc:
            raise LLMError(TRANSIENT, f"connection: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            outcome, retry = classify(resp.status_code, _error_message(resp), resp.headers, self.provider.id)
            raise LLMError(outcome, "models list failed", status=resp.status_code, retry_after=retry)
        data = _json(resp)
        items = data.get("data") if isinstance(data, dict) else data
        out: dict[str, dict] = {}
        for m in items or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            pricing = m.get("pricing") or {}
            price = None
            raw_price = pricing.get("completion", pricing.get("output"))
            try:
                if raw_price is not None:
                    price = float(raw_price)
                    if "completion" in pricing:  # OpenRouter: USD per token
                        price *= 1_000_000
            except (TypeError, ValueError):
                price = None
            params = m.get("supported_parameters")
            tools = ("tools" in params) if isinstance(params, list) else None
            context = m.get("context_length") or m.get("context_window") or (m.get("top_provider") or {}).get(
                "context_length")
            kind = str(m.get("type") or (m.get("architecture") or {}).get("modality") or "")
            out[str(m["id"])] = {"id": str(m["id"]), "price_out": price, "context": int(context) if context else None,
                                 "tools": tools, "kind": kind}
        return sorted(out.values(), key=lambda x: x["id"])

    async def models(self) -> list[str]:
        return [m["id"] for m in await self.models_meta()]


# ------------------------------------------------------------------ Anthropic (official SDK)

def _anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant":
            raw = m.get("_raw")
            if raw is not None:  # replay the model's own blocks (thinking included) unchanged
                out.append({"role": "assistant", "content": raw})
                continue
            out.append({"role": "assistant", "content": [
                b if b.get("type") != "tool_use" else {"type": "tool_use", "id": b["id"], "name": b["name"],
                                                       "input": b.get("input") or {}}
                for b in (m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}])]})
        else:
            content = m["content"]
            if isinstance(content, list):
                content = [{k: v for k, v in b.items() if not (k == "is_error" and not v)} for b in content]
            out.append({"role": "user", "content": content})
    return out


class AnthropicClient:
    def __init__(self, provider: Provider, secret: str, base_url: str | None = None, *, http_client=None,
                 timeout: float = 600):
        import anthropic

        self._anthropic = anthropic
        kwargs: dict[str, Any] = {"api_key": secret, "max_retries": 0, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.provider = provider
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def close(self) -> None:
        await self.client.close()

    def _error(self, exc: Exception) -> LLMError:
        a = self._anthropic
        if isinstance(exc, (a.APITimeoutError, a.APIConnectionError)):
            return LLMError(TRANSIENT, type(exc).__name__)
        if isinstance(exc, a.APIStatusError):
            status = exc.status_code
            message = str(getattr(exc, "message", "") or "")
            headers = getattr(getattr(exc, "response", None), "headers", None)
            credit_errors = tuple(getattr(a, n) for n in (
                "PaymentRequiredError", "EnforcedSpendLimitReachedError", "ContractSpendLimitReachedError",
                "LinkedOrganizationSpendLimitReachedError", "OrganizationOnHoldError") if hasattr(a, n))
            if credit_errors and isinstance(exc, credit_errors):
                return LLMError(NO_CREDITS, message, status=status)
            outcome, retry = classify(status, message, headers, "anthropic")
            return LLMError(outcome, message, status=status, retry_after=retry)
        return LLMError(TRANSIENT, type(exc).__name__)

    async def create(self, req: Request) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": req.model, "max_tokens": req.max_tokens,
                                  "messages": _anthropic_messages(req.messages)}
        if req.system:
            kwargs["system"] = req.system
        if req.tools:
            kwargs["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                                "input_schema": t.get("input_schema") or {"type": "object", "properties": {}}}
                               for t in req.tools]
        if req.effort:
            kwargs["output_config"] = {"effort": req.effort}
        kwargs["cache_control"] = {"type": "ephemeral"}  # agent loops resend the same prefix
        try:
            if req.model in _FALLBACK_MODELS:
                # a safety-classifier refusal is retried server-side on a fallback model
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = await self.client.messages.create(**kwargs)
        except self._anthropic.AnthropicError as exc:
            raise self._error(exc) from exc
        raw = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})})
        stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}.get(resp.stop_reason or "", "end")
        usage = resp.usage
        tokens_in = int((usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                        + (getattr(usage, "cache_creation_input_tokens", 0) or 0))
        return LLMResponse(blocks, stop, tokens_in, int(usage.output_tokens or 0), raw=raw, model=resp.model)

    async def probe(self, model: str | None) -> None:
        try:
            if model:
                await self.client.messages.create(model=model, max_tokens=16,
                                                  messages=[{"role": "user", "content": "ping"}])
            else:
                await self.client.models.list(limit=1)
        except self._anthropic.AnthropicError as exc:
            raise self._error(exc) from exc

    async def models(self) -> list[str]:
        try:
            page = await self.client.models.list(limit=100)
        except self._anthropic.AnthropicError as exc:
            raise self._error(exc) from exc
        return sorted(m.id for m in page.data)

    async def models_meta(self) -> list[dict]:
        return [{"id": m, "price_out": None, "context": None, "tools": True, "kind": "text"} for m in await self.models()]


# test hook: replace to serve fake providers
CLIENT_FACTORY = None


def make_client(provider: Provider, secret: str, base_url: str | None = None):
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(provider, secret, base_url)
    if provider.kind == "anthropic":
        return AnthropicClient(provider, secret, base_url)
    return OpenAICompatible(provider, secret, base_url)
