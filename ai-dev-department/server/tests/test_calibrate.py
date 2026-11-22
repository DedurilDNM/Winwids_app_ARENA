"""Account self-calibration against fake service pages (real browser).

The fake page answers the calibration message like a model would (it computes
the sums), streams the answer, shows a Stop button while busy, and lays out
messages in different ways: with data attributes, with classes, or identical
for both sides (then only the «anchor» reader can work)."""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

CHROME = os.environ.get("E2E_CHROMIUM") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
pytestmark = pytest.mark.skipif(not (os.path.exists(CHROME) or shutil.which("chromium")), reason="no browser")

SERVICE = "https://service.example.com/"


def _page(variant: str, busy: bool = True, hidden_old: bool = False) -> str:
    user_attrs = {"data": 'data-author="user"', "class": 'class="row"', "plain": ""}[variant]
    bot_attrs = {"data": 'data-author="assistant"', "class": 'class="row"', "plain": ""}[variant]
    body_cls = {"data": "", "class": ' class="answer"', "plain": ""}[variant]
    old = ("<section style='display:none'><div><p>AIDD-CHECK-OLD old prompt</p></div><div><p>QX1 old</p></div></section>"
           if hidden_old else "")
    return """<!doctype html><meta charset="utf-8">
<main><div id="list"></div></main>""" + old + """
<textarea id="box"></textarea><button id="send">Send</button>
<footer><p>Answers may contain mistakes.</p></footer>
<script>
const list = document.getElementById("list");
const BUSY = """ + ("true" if busy else "false") + """;
document.getElementById("send").onclick = () => {
  const text = document.getElementById("box").value; document.getElementById("box").value = "";
  const u = document.createElement("div"); u.innerHTML = '<div USER><div><p></p></div></div>'.replace("USER", '""" + user_attrs.replace("'", "\\'") + """');
  u.querySelector("p").textContent = text; list.append(u.firstChild);
  let stop = null;
  if (BUSY) { stop = document.createElement("button"); stop.setAttribute("aria-label", "Stop generating");
              stop.textContent = "■"; document.body.append(stop); }
  const sums = [...text.matchAll(/sum of (\\d+) and (\\d+)/g)].map((m) => Number(m[1]) + Number(m[2]));
  const isCheck = sums.length === 2;
  const row = document.createElement("div"); row.innerHTML = '<div BOT><div class="head"><span>model-x</span></div><div BODY></div></div>'
    .replace("BOT", '""" + bot_attrs.replace("'", "\\'") + """').replace("BODY", '""" + body_cls.replace("'", "\\'").strip() + """');
  const msg = row.firstChild; const body = msg.lastChild;
  setTimeout(() => list.append(msg), 600);   // «thinking» first
  const parts = isCheck
    ? ["<p>QX" + sums[0] + "</p>",
       "<div class='code'><div class='hdr'><span>text</span><button>Copy</button></div><pre><code>ZW" + sums[1] + "</code></pre></div>",
       "<p>STATUS: DONE</p>"]
    : ["<p>FILE: a.txt</p>", "<pre><code>hello</code></pre>", "<p>STATUS: DONE</p><p>SUMMARY: made a.txt</p>"];
  parts.forEach((html, i) => setTimeout(() => { body.insertAdjacentHTML("beforeend", html);
    if (i === parts.length - 1 && stop) setTimeout(() => stop.remove(), 400); }, 1200 + i * 700));
};
</script>"""


def _settings(**kw):
    from app.config import Settings

    base = dict(SERVICE_URL=SERVICE, ADAPTER_INPUT_SELECTOR="#box", ADAPTER_SEND_SELECTOR="#send",
                ADAPTER_LOGIN_SELECTOR="#login", ADAPTER_MESSAGE_SELECTOR=".no-such-message",
                ADAPTER_STATE_SELECTOR=".no-such-busy", ADAPTER_POLL_INTERVAL_SECONDS=0.2,
                ADAPTER_SETTLE_SECONDS=1.0, ADAPTER_AFTER_REPLY_ESCAPE=False)
    base.update(kw)
    return Settings(**base)


def _calibrate(html: str):
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import Adapter
    from app.adapters.generic.calibrate import calibrate

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

            await context.route("https://service.example.com/**", serve)
            adapter = Adapter(settings=_settings())
            profile = await calibrate(adapter, context, timeout=30)

            # then a normal task in a new chat with the learned profile
            adapter2 = Adapter(settings=_settings())
            adapter2.profile = profile
            page = await adapter2.open_chat(context, "task-1")
            states = []

            async def watch():
                for _ in range(20):
                    states.append(await adapter2.get_state(page))
                    await asyncio.sleep(0.2)

            await adapter2.send_message(page, "You are the Coder. Write a.txt")
            watcher = asyncio.create_task(watch())
            reply = await adapter2.wait_response(page, timeout=30)
            await watcher
            await browser.close()
            return profile, reply, states

    return asyncio.run(go())


@pytest.mark.parametrize("variant", ["data", "class"])
def test_calibration_learns_a_message_selector_and_the_busy_indicator(variant):
    profile, reply, states = _calibrate(_page(variant))
    assert profile["ok"], profile
    assert profile["reader"] == "selector" and profile["message"]
    assert profile["busy"] == 'button[aria-label="Stop generating"]'
    assert profile["checks"] == {"markers": True, "code_block": True, "status_line": True, "own_message_found": True}
    assert "STATUS: DONE" in reply and "SUMMARY: made a.txt" in reply and "AIDD-CHECK" not in reply
    assert "generating" in states  # the learned busy indicator works in a normal task


def test_identical_layout_falls_back_to_the_anchor_reader_and_no_busy_indicator():
    profile, reply, states = _calibrate(_page("plain", busy=False, hidden_old=True))
    assert profile["ok"], profile
    assert profile["reader"] == "anchor" and profile["message"] == ""
    assert profile["busy"] == ""
    assert "SUMMARY: made a.txt" in reply and "Answers may contain" not in reply


def test_profile_selectors_override_the_env_ones():
    from app.adapters.generic.adapter import Adapter

    a = Adapter(settings=_settings())
    assert a._selectors["message"] == ".no-such-message"
    a.profile = {"ok": True, "reader": "selector", "message": "div[data-author=\"assistant\"]", "busy": "", "input": ""}
    assert a._selectors["message"] == 'div[data-author="assistant"]' and a._selectors["state"] == ""
    assert a._selectors["input"] == "#box"
    a.profile = {"ok": True, "reader": "anchor", "message": "", "busy": "button.x"}
    assert a._selectors["message"] == "" and a._selectors["state"] == "button.x"
