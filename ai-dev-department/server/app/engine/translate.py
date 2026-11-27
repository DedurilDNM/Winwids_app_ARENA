"""Anthropic Messages API <-> OpenAI chat completions, for the engine's proxy.

Claude Code speaks only the Anthropic API. For an aggregator that speaks only the OpenAI
protocol (Groq, Gemini, Mistral, OpenRouter, Together, Cerebras, …) the proxy turns
Claude Code's request into a chat-completions request and the answer - streamed or not,
text and tool calls - back into Anthropic's format, event by event.

Dropped on the way (the OpenAI protocol has no place for them): thinking blocks, cache
control, server tools (web search), effort/thinking settings.
"""

from __future__ import annotations

import json
import time
from typing import Any

MAX_TOKENS_CAP = 16384  # Claude Code asks for 32-64k; most OpenAI-protocol models allow less
STOP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
        "content_filter": "refusal"}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _image_part(block: dict) -> dict | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def to_openai(body: dict) -> dict:
    """An Anthropic /v1/messages body -> an OpenAI /chat/completions body."""
    out_msgs: list[dict] = []
    system = _text_of(body.get("system") or "")
    if system:
        out_msgs.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        content = m.get("content")
        if m.get("role") == "assistant":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b.get("id"), "type": "function", "function": {
                "name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                for b in blocks if b.get("type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            if text or calls:
                out_msgs.append(msg)
            continue
        if isinstance(content, str):
            out_msgs.append({"role": "user", "content": content})
            continue
        parts: list[dict] = []
        later_images: list[dict] = []
        for b in content or []:
            kind = b.get("type")
            if kind == "tool_result":
                body_ = b.get("content")
                text = body_ if isinstance(body_, str) else _text_of(body_)
                if isinstance(body_, list):  # tool messages take text only: the pictures follow as the user's
                    later_images += [p for p in (_image_part(x) for x in body_ if x.get("type") == "image") if p]
                out_msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                                 "content": ("ERROR: " if b.get("is_error") else "") + (text or "(no output)")})
            elif kind == "text" and b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
            elif kind == "image":
                img = _image_part(b)
                if img:
                    parts.append(img)
            elif kind == "document":
                src = b.get("source") or {}
                if src.get("type") == "text":
                    parts.append({"type": "text", "text": src.get("data", "")})
        parts += later_images
        if parts:
            if all(p["type"] == "text" for p in parts):
                out_msgs.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                out_msgs.append({"role": "user", "content": parts})
    out: dict[str, Any] = {"model": body.get("model"), "messages": out_msgs,
                           "max_tokens": min(int(body.get("max_tokens") or 4096), MAX_TOKENS_CAP)}
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", "")[:4000],
                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
             for t in body.get("tools") or [] if t.get("name") and "input_schema" in t]  # no server tools
    if tools:
        out["tools"] = tools
        choice = body.get("tool_choice") or {}
        if choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif choice.get("type") == "tool" and choice.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif choice.get("type") == "none":
            out["tool_choice"] = "none"
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"][:4]
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def _usage(u: dict | None) -> dict:
    u = u or {}
    return {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}


def from_openai(data: dict, model: str) -> dict:
    """A whole OpenAI answer -> an Anthropic message."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    text = msg.get("content") if isinstance(msg.get("content"), str) else _text_of(msg.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({"type": "tool_use", "id": call.get("id") or f"toolu_{time.time_ns()}", "name": fn.get("name", ""),
                        "input": args if isinstance(args, dict) else {}})
    stop = STOP.get(choice.get("finish_reason") or "stop", "end_turn")
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    return {"id": data.get("id") or f"msg_{time.time_ns()}", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None, "usage": _usage(data.get("usage"))}


def _sse(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class StreamTranslator:
    """OpenAI stream chunks in, Anthropic stream events out."""

    def __init__(self, model: str):
        self.model = model
        self.started = False
        self.index = -1
        self.open: str | None = None  # "text" | "tool:<openai index>"
        self.tools: dict[int, int] = {}  # openai tool index -> our block index
        self.stop = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.buf = bytearray()
        self.done = False

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": f"msg_{time.time_ns()}", "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 1}}})]

    def _close(self) -> list[bytes]:
        if self.open is None:
            return []
        self.open = None
        return [_sse("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def feed(self, raw: bytes) -> list[bytes]:
        """Raw bytes of the OpenAI stream (any split) -> Anthropic events."""
        self.buf.extend(raw)
        out: list[bytes] = []
        while b"\n" in self.buf:
            line, _, rest = bytes(self.buf).partition(b"\n")
            self.buf[:] = rest
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                out += self.finish()
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            out += self.chunk(chunk)
        return out

    def chunk(self, chunk: dict) -> list[bytes]:
        out = self._start()
        if chunk.get("usage"):
            self.usage = _usage(chunk["usage"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self.open != "text":
                    out += self._close()
                    self.index += 1
                    self.open = "text"
                    out.append(_sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                            "content_block": {"type": "text", "text": ""}}))
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "text_delta", "text": text}}))
            for call in delta.get("tool_calls") or []:
                i = int(call.get("index") or 0)
                fn = call.get("function") or {}
                if i not in self.tools:
                    out += self._close()
                    self.index += 1
                    self.tools[i] = self.index
                    self.open = f"tool:{i}"
                    out.append(_sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                            "content_block": {"type": "tool_use",
                                                                              "id": call.get("id") or f"toolu_{time.time_ns()}",
                                                                              "name": fn.get("name", ""), "input": {}}}))
                if fn.get("arguments"):
                    out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.tools[i],
                                                            "delta": {"type": "input_json_delta",
                                                                      "partial_json": fn["arguments"]}}))
            if choice.get("finish_reason"):
                self.stop = STOP.get(choice["finish_reason"], "end_turn")
        if self.tools and self.stop == "end_turn":
            self.stop = "tool_use"
        return out

    def finish(self) -> list[bytes]:
        if self.done:
            return []
        self.done = True
        out = self._start() + self._close()
        out.append(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": self.stop, "stop_sequence": None},
                                          "usage": {"input_tokens": self.usage["input_tokens"],
                                                    "output_tokens": self.usage["output_tokens"]}}))
        out.append(_sse("message_stop", {"type": "message_stop"}))
        return out
