"""Agents look at their pages: the Screenshot tool, images in Read, images reaching the model
(Anthropic and OpenAI-compatible formats), and models that do not take images."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents import llm, screenshot
from app.agents.department import ROLE_TOOLS, _role_tools
from app.agents.llm import LLMError, LLMResponse
from app.agents.runtime import AgentSpec, drop_images, run_agent
from app.agents.tools import ToolContext

from tests.test_agent_runtime import Script, _call, _ctx

CHROMIUM = os.environ.get("AGENT_SCREENSHOT_CHROMIUM") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"


def _png(path: Path, size=(1440, 900), colour=(30, 30, 28)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)


@pytest.fixture
def fake_browser(monkeypatch):
    seen = []

    async def engine(url, width, height, scale, full_page, out):
        seen.append((url, width, height))
        _png(out, (width * scale, height * scale))

    monkeypatch.setattr(screenshot, "ENGINE", engine)
    return seen


def test_the_agent_takes_screenshots_and_sees_them(tmp_path, fake_browser):
    script = Script([_call("Screenshot", path="prototype")],
                    [_call("Read", 1, path="screenshots/prototype-phone.png")], "Looks right")
    ctx, events = _ctx(tmp_path)
    (ctx.workspace / "prototype").mkdir()
    (ctx.workspace / "prototype/index.html").write_text("<h1>hi</h1>")
    spec = AgentSpec(role="Coder", provider="custom", model="m", tools=["Screenshot", "Read"])
    result = asyncio.run(run_agent(spec, ctx, "Check the page", script))
    assert result.stopped == "done"
    shot = script.results(1)[0]["content"]
    assert shot[0]["type"] == "text" and "desktop: screenshots/prototype-desktop.png (1280x800)" in shot[0]["text"]
    assert [b["type"] for b in shot] == ["text", "image", "image"]
    assert shot[1]["source"]["media_type"] == "image/jpeg" and len(shot[1]["source"]["data"]) > 100  # scaled down
    assert [(w, h) for _, w, h in fake_browser] == [(1440, 900), (390, 844)]
    assert fake_browser[0][0].endswith("/prototype/index.html")
    read = script.results(2)[0]["content"]
    assert read[1]["type"] == "image" and "591x1280" in read[0]["text"]
    # the chat shows a plain line, never the image data
    done = [e for e in events if e["type"] == "tool_done"]
    assert all("base64" not in str(e) and len(str(e)) < 3000 for e in done)


def test_screenshot_needs_a_page_of_the_project(tmp_path, fake_browser):
    script = Script([_call("Screenshot", path="../etc/passwd")], [_call("Screenshot", 1, path="notes.txt")], "ok")
    ctx, _ = _ctx(tmp_path)
    (ctx.workspace / "notes.txt").write_text("x")
    spec = AgentSpec(role="Coder", provider="custom", model="m", tools=["Screenshot"])
    asyncio.run(run_agent(spec, ctx, "go", script))
    assert script.results(1)[0]["is_error"] and script.results(2)[0]["is_error"]
    assert "html" in script.results(2)[0]["content"] and not fake_browser


def test_big_images_are_scaled_down(tmp_path):
    _png(tmp_path / "big.png", (4000, 3000))
    kind, data, size = screenshot.image_block(tmp_path / "big.png")
    assert kind == "image/jpeg" and max(size) == screenshot.MAX_SIDE


def test_the_page_server_gives_only_the_project_folder(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "page.html").write_text("<p>привет</p>")
    (tmp_path / "secret.txt").write_text("server secret")
    (ws / "link.txt").symlink_to(tmp_path / "secret.txt")
    with screenshot._Serve(ws) as srv:
        base = f"http://127.0.0.1:{srv.port}/"
        page = urllib.request.urlopen(base + "page.html")
        assert page.headers["Content-Type"] == "text/html; charset=utf-8"
        for bad in ("link.txt", "../secret.txt", "%2e%2e/secret.txt"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + bad)
    assert screenshot._private("127.0.0.1") and screenshot._private("10.1.2.3") and screenshot._private("localhost")
    assert not screenshot._private("fonts.googleapis.com") and not screenshot._private("8.8.8.8")


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="no Chromium here")
def test_a_real_screenshot_and_no_requests_to_local_services(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SCREENSHOT_CHROMIUM", CHROMIUM)
    hits = []

    class Spy(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    spy = ThreadingHTTPServer(("127.0.0.1", 0), Spy)
    threading.Thread(target=spy.serve_forever, daemon=True).start()
    ws = tmp_path / "ws"
    (ws / "p").mkdir(parents=True)
    (ws / "p/index.html").write_text(
        '<html><head><link rel="stylesheet" href="s.css"></head><body><h1>Привет</h1>'
        f'<img src="http://127.0.0.1:{spy.server_address[1]}/img">'
        f'<script>fetch("http://127.0.0.1:{spy.server_address[1]}/api").catch(()=>{{}})</script></body></html>')
    (ws / "p/s.css").write_text("body{background:rgb(200,0,0)}")
    try:
        shots = asyncio.run(screenshot.take(ws, "p/index.html", ws / "screenshots", viewports=["desktop"], full_page=False))
    finally:
        spy.shutdown()
    from PIL import Image

    with Image.open(shots[0]) as im:
        assert im.size == (1440, 900) and im.getpixel((1400, 880))[:3] == (200, 0, 0)  # the CSS file loaded
    assert hits == []  # the page could not reach a local service


# ------------------------------------------------------------------ images in the conversation

def _image_result(i=0):
    return {"type": "tool_result", "tool_use_id": f"c{i}", "is_error": False, "content": [
        {"type": "text", "text": f"Screenshots {i}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": f"AAA{i}"}}]}


def test_openai_format_puts_images_after_the_tool_messages():
    messages = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": [_call("Screenshot", 0, path="a.html"), _call("Read", 1, path="b.png")]},
                {"role": "user", "content": [_image_result(0), _image_result(1)]}]
    for m in messages[1]["content"]:
        m["id"] = {"Screenshot": "c0", "Read": "c1"}[m["name"]]
    out = llm._to_openai_messages("sys", messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert out[3]["content"] == "Screenshots 0" and out[4]["content"] == "Screenshots 1"
    images = [p for p in out[5]["content"] if p["type"] == "image_url"]
    assert [p["image_url"]["url"] for p in images] == ["data:image/png;base64,AAA0", "data:image/png;base64,AAA1"]


def test_anthropic_format_keeps_images_in_tool_results():
    out = llm._anthropic_messages([{"role": "user", "content": [_image_result(0)]}])
    assert out[0]["content"][0]["content"][1]["type"] == "image"


def test_old_images_leave_the_conversation():
    messages = [{"role": "user", "content": [_image_result(i)]} for i in range(6)]
    assert drop_images(messages, keep=4) == 2
    kinds = [m["content"][0]["content"][1]["type"] for m in messages]
    assert kinds == ["text", "text", "image", "image", "image", "image"]


def test_a_model_without_vision_goes_on_without_images(tmp_path, fake_browser):
    calls = []

    async def complete(spec, req):
        has_image = any(isinstance(b.get("content"), list) and any(p.get("type") == "image" for p in b["content"])
                        for m in req.messages if isinstance(m["content"], list) for b in m["content"])
        calls.append(has_image)
        if has_image:
            raise LLMError(llm.REQUEST, "image input is not supported by this model", status=400)
        if len(calls) == 1:
            return LLMResponse([_call("Screenshot", path="a.html")], "tool_use", 1, 1)
        return LLMResponse([{"type": "text", "text": "checked the code instead"}], "end", 1, 1)

    ctx, _ = _ctx(tmp_path)
    (ctx.workspace / "a.html").write_text("<p>x</p>")
    spec = AgentSpec(role="Coder", provider="custom", model="m", tools=["Screenshot"])
    result = asyncio.run(run_agent(spec, ctx, "go", complete))
    assert result.stopped == "done" and calls == [False, True, False]


# ------------------------------------------------------------------ new tools for saved roles

def test_roles_saved_earlier_get_new_tools_but_unchecked_ones_stay_off():
    old = SimpleNamespace(role="Coder", tools=["Read", "Write"], tools_seen=None)
    assert "Screenshot" in _role_tools(old) and "Bash" not in _role_tools(old)  # Bash was known then: left off
    from app.agents.tools import ALL_TOOL_NAMES

    after = SimpleNamespace(role="Coder", tools=["Read", "Write"], tools_seen=list(ALL_TOOL_NAMES))
    assert _role_tools(after) == ["Read", "Write"]
    default = SimpleNamespace(role="Reviewer", tools=None, tools_seen=None)
    assert _role_tools(default) == ROLE_TOOLS["Reviewer"]
