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
        if "прототип" in prompt.lower():
            if turn == 0:
                html = ("<!doctype html><meta charset=utf-8><title>Таймер</title><body style='font-family:sans-serif'>"
                        "<h1 id=t>25:00</h1><button id=go onclick=\"document.getElementById('t').textContent='24:59'\">Старт</button>")
                xlsx = ("from openpyxl import Workbook; wb=Workbook(); ws=wb.active; ws.title='Итоги'; "
                        "ws.append(['Месяц','Сессий']); ws.append(['Январь',42]); ws.append(['Февраль',57]); "
                        "import os; os.makedirs('out', exist_ok=True); wb.save('out/report.xlsx')")
                return {"content": None, "tool_calls": [call("Write", 0, path="prototype/index.html", content=html),
                                                        call("Bash", 1, command=f'python3 -c "{xlsx}"')]}
            if turn == 1:  # look at the page
                return {"content": None, "tool_calls": [call("Screenshot", 0, path="prototype/index.html", viewport="desktop")]}
            if turn == 2:
                saw = any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url"
                          and p["image_url"]["url"].startswith("data:image/") for p in m["content"]) for m in messages)
                if not saw:
                    return {"content": "Скриншот не дошёл до модели."}
                return {"content": None, "tool_calls": [call("SendFile", 0, path="prototype/index.html", caption="Главный экран"),
                                                        call("SendFile", 1, path="out/report.xlsx"),
                                                        call("SendFile", 2, path="screenshots/prototype-desktop.png", caption="Скриншот")]}
            return {"content": "Прототип и таблица готовы."}
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


# ------------------------------------------------------------------ Anthropic format (the Claude Code engine)
ANTHROPIC_LOG: list[dict] = []
PAGE = ("<!doctype html><html lang=ru><head><meta charset=utf-8><title>Вход</title><style>body{background:#111;"
        "color:#eee;font:16px sans-serif;display:grid;place-items:center;height:100vh;margin:0}</style></head>"
        "<body><form><h1>Вход</h1><input id=email placeholder=E-mail><button id=go type=button "
        "onclick=\"this.textContent='Вошли'\">Войти</button></form></body></html>")


def _text_of(messages) -> str:
    return json.dumps(messages, ensure_ascii=False)


def anthropic_reply(body: dict) -> list[dict]:
    """Plays Claude in Claude Code: plan, give the page to the coder, look at it, send it; a follow-up
    turn (resumed session) edits the page."""
    msgs = body.get("messages") or []
    tools = [t.get("name") for t in body.get("tools") or []]
    text = _text_of(msgs)
    if not tools:  # side requests (titles, summaries)
        return [{"type": "text", "text": "ok"}]
    asst = [m for m in msgs if m.get("role") == "assistant"]
    tid = lambda: f"toolu_{time.time_ns()}"  # noqa: E731
    if "SUBTASK" in _text_of(msgs[:1]):  # the coder sub-agent
        if not asst:
            return [{"type": "text", "text": "Пишу страницу."},
                    {"type": "tool_use", "id": tid(), "name": "Write",
                     "input": {"file_path": "prototype/index.html", "content": PAGE}}]
        return [{"type": "text", "text": "Файл готов: prototype/index.html."}]
    last_user = next((m for m in reversed(msgs) if m.get("role") == "user"
                      and isinstance(m.get("content"), str) or (isinstance(m.get("content"), list)
                      and any(b.get("type") == "text" for b in m["content"]))), {})
    if "темнее" in _text_of([last_user]) and "Сделал темнее" not in text:
        remembers = "Готово: прототип" in text
        if '"Edit"' not in _text_of(msgs[-3:]) and "has been updated" not in _text_of(msgs[-2:]):
            return [{"type": "tool_use", "id": tid(), "name": "Edit", "input": {
                "file_path": "prototype/index.html", "old_string": "background:#111", "new_string": "background:#000"}}]
        return [{"type": "text", "text": "Сделал темнее." + (" (помню прошлый ход)" if remembers else " (НЕ помню)")}]
    if "Файл готов" not in text:
        if not asst:
            return [{"type": "text", "text": "Беру задачу: план и кодер."},
                    {"type": "tool_use", "id": tid(), "name": "TodoWrite", "input": {"todos": [
                        {"content": "Сверстать экран входа", "status": "in_progress", "activeForm": "Верстаю"},
                        {"content": "Посмотреть скриншоты", "status": "pending", "activeForm": "Смотрю"}]}},
                    {"type": "tool_use", "id": tid(), "name": "Agent", "input": {
                        "subagent_type": "coder", "description": "вёрстка", "prompt": "SUBTASK: сверстай prototype/index.html"}}]
        return [{"type": "text", "text": "Жду кодера."}]
    if "Screenshots (attached" not in text:
        return [{"type": "tool_use", "id": tid(), "name": "mcp__aidd__Screenshot",
                 "input": {"path": "prototype/index.html", "viewport": "desktop"}}]
    if "Shown to the user" not in text:
        saw = '"type": "image"' in text
        return [{"type": "tool_use", "id": tid(), "name": "mcp__aidd__SendFile",
                 "input": {"path": "prototype/index.html", "caption": "Экран входа" if saw else "БЕЗ КАРТИНКИ"}}]
    return [{"type": "text", "text": "Готово: прототип в prototype/index.html, проверил по скриншоту."}]


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
        if not self._key() and not self.path.startswith("/anthropic-log"):
            return self._send(401, {"error": {"message": "no key"}})
        if self.path.startswith("/anthropic-log"):
            return self._send(200, {"requests": ANTHROPIC_LOG})
        if self.path.endswith("/models"):
            return self._send(200, {"data": [{"id": "fake-model"}, {"id": "fake-model-mini"}]})
        self._send(404, {"error": {"message": "not found"}})

    def _anthropic(self, body: dict):
        key = self.headers.get("x-api-key") or self._key()
        ANTHROPIC_LOG.append({"model": body.get("model"), "key": key[-4:], "output_config": body.get("output_config"),
                              "thinking": body.get("thinking"), "tools": [t.get("name") for t in body.get("tools") or []]})
        if not key:
            return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "no key"}})
        if key.startswith("sk-limited"):
            return self._send(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "limited"}},
                              {"Retry-After": "120"})
        if self.path.startswith("/v1/messages/count_tokens"):
            return self._send(200, {"input_tokens": 100})
        content = anthropic_reply(body)
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        msg = {"id": f"msg_{time.time_ns()}", "type": "message", "role": "assistant", "model": body.get("model"),
               "content": content, "stop_reason": stop, "stop_sequence": None,
               "usage": {"input_tokens": 200, "output_tokens": 40}}
        if not body.get("stream"):
            return self._send(200, msg)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def ev(kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        ev("message_start", {"type": "message_start", "message": {**msg, "content": [], "stop_reason": None,
                                                                    "usage": {"input_tokens": 200, "output_tokens": 1}}})
        for i, c in enumerate(content):
            if c["type"] == "text":
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": c["text"]}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(c["input"], ensure_ascii=False)}})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 40}})
        ev("message_stop", {"type": "message_stop"})
        self.close_connection = True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.startswith("/v1/messages"):
            return self._anthropic(body)
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
