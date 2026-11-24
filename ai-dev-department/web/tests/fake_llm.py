"""A fake OpenAI-compatible aggregator for e2e tests of the API department.

    python3 fake_llm.py 9200

Keys starting with "sk-limited" always get 429 (rate limit); other keys work. The
model plays a small department: the Orchestrator plans and gives work to a Coder
and a Tester in one reply (parallel), then reports; the Coder writes a file; the
Tester runs a command."""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def call(name, n, **args):
    return {"id": f"{name}-{n}-{time.time_ns()}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def reply(role: str, turn: int, messages: list) -> dict:
    if role == "Orchestrator":
        if turn == 0:
            return {"content": "Разбиваю задачу: кодер пишет, тестер проверяет.", "tool_calls": [
                call("TodoWrite", 0, todos=[{"content": "Написать hello.py", "status": "in_progress"},
                                            {"content": "Проверить запуск", "status": "pending"}]),
                call("Task", 1, role="Coder", description="hello.py", prompt=next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m.get("content"), str)),
                    "Create hello.py")),
                call("Task", 2, role="Tester", description="проверка", prompt="Run hello.py and report")]}
        return {"content": "Готово: **hello.py** написан, тестер проверил запуск."}
    if role == "Coder":
        prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        if turn == 0 and "тёмн" in prompt.lower():
            return {"content": None, "tool_calls": [call("Edit", 0, path="hello.py", old_string="Hello", new_string="Hello, dark")]}
        if turn == 0:
            time.sleep(1.0)
            return {"content": None, "tool_calls": [call("Write", 0, path="hello.py", content="print('Hello')\n")]}
        return {"content": "Создал hello.py."}
    if role == "Tester":
        if turn == 0:
            return {"content": None, "tool_calls": [call("Bash", 0, command="sleep 1.5; ls")]}
        return {"content": "Проверил: файлы на месте."}
    return {"content": "ok"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: dict, headers: dict | None = None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _key(self) -> str:
        return (self.headers.get("Authorization") or "").split(" ")[-1]

    def do_GET(self):
        if not self._key():
            return self._send(401, {"error": {"message": "no key"}})
        if self.path.endswith("/models"):
            return self._send(200, {"data": [{"id": "fake-model"}, {"id": "fake-model-mini"}]})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        key = self._key()
        if key.startswith("sk-limited"):
            return self._send(429, {"error": {"message": "rate limited"}}, {"Retry-After": "120"})
        messages = body.get("messages") or []
        if body.get("max_tokens") == 1:  # a key check
            return self._send(200, {"choices": [{"message": {"content": "p"}, "finish_reason": "length"}]})
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        tools = [t["function"]["name"] for t in body.get("tools") or []]
        if not system and "report_ready" in tools:  # the model picker's check: call the tool
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                call("report_ready", 0, ready=True)]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5}})
        role = (re.search(r"You are the (\w+)", system) or [None, "?"])[1]
        # replies since the current prompt (a chat sends the earlier messages first)
        last = max((i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m.get("content"), str)),
                   default=0)
        turn = sum(1 for m in messages[last:] if m["role"] == "assistant")
        msg = reply(role, turn, messages)
        finish = "tool_calls" if msg.get("tool_calls") else "stop"
        self._send(200, {"model": body.get("model"), "choices": [{"message": {"role": "assistant", **msg},
                                                                   "finish_reason": finish}],
                         "usage": {"prompt_tokens": 120 + 40 * turn, "completion_tokens": 30}})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9200), Handler).serve_forever()
