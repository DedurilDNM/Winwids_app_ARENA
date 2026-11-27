"""The engine proxy's Anthropic <-> OpenAI translation (Claude Code on Groq, Gemini, Mistral …)."""

from __future__ import annotations

import json

from app.engine import translate


def _events(raw: list[bytes]) -> list[dict]:
    out = []
    for e in b"".join(raw).decode().split("\n\n"):
        for line in e.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_a_claude_code_request_becomes_chat_completions():
    body = {"model": "llama-3.3-70b", "max_tokens": 64000, "stream": True, "temperature": 1,
            "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
            "tools": [{"name": "Write", "description": "write", "input_schema": {"type": "object", "properties": {}}},
                      {"type": "web_search_20250305", "name": "web_search"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Сделай файл"},
                                             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}]},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": "Пишу"},
                                                  {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "a"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "ok"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "BBB"}}]}]}]}
    out = translate.to_openai(body)
    assert out["max_tokens"] == translate.MAX_TOKENS_CAP and out["stream"] and out["stream_options"] == {"include_usage": True}
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "tool", "user"]
    assert out["messages"][0]["content"] == "You are Claude Code."
    assert out["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"
    assert out["messages"][2]["tool_calls"][0]["function"] == {"name": "Write", "arguments": '{"file_path": "a"}'}
    assert out["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}
    assert out["messages"][4]["content"][0]["image_url"]["url"].startswith("data:image/jpeg")  # the tool's picture
    assert [t["function"]["name"] for t in out["tools"]] == ["Write"]  # no server tools
    assert "thinking" not in out and "output_config" not in out


def test_an_openai_stream_becomes_anthropic_events_split_anywhere():
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"content": "Пиш"}}]}, {"choices": [{"delta": {"content": "у."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "Write", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"file_path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "a.txt"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 9}}]
    raw = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"
    tr = translate.StreamTranslator("m")
    out = []
    for i in range(0, len(raw), 7):  # bytes arrive in small pieces
        out += tr.feed(raw[i:i + 7])
    ev = _events(out)
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "message_start" and kinds[-2:] == ["message_delta", "message_stop"]
    text = "".join(e["delta"]["text"] for e in ev if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
    args = "".join(e["delta"]["partial_json"] for e in ev if e["type"] == "content_block_delta"
                   and e["delta"]["type"] == "input_json_delta")
    assert text == "Пишу." and json.loads(args) == {"file_path": "a.txt"}
    starts = [e["content_block"] for e in ev if e["type"] == "content_block_start"]
    assert [s["type"] for s in starts] == ["text", "tool_use"] and starts[1]["name"] == "Write" and starts[1]["id"] == "call_1"
    assert kinds.count("content_block_stop") == 2
    assert ev[-2]["delta"]["stop_reason"] == "tool_use" and ev[-2]["usage"] == {"input_tokens": 120, "output_tokens": 9}


def test_a_whole_openai_answer_becomes_a_message():
    msg = translate.from_openai({"choices": [{"message": {"content": "Готово", "tool_calls": [
        {"id": "c", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, "m")
    assert msg["content"][0] == {"type": "text", "text": "Готово"}
    assert msg["content"][1]["input"] == {"command": "ls"} and msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 5, "output_tokens": 2}
