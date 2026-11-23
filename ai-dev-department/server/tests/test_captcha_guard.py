"""A captcha on the page is never navigated away from or reloaded while a human solves it."""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

CHROME = os.environ.get("E2E_CHROMIUM") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
pytestmark = pytest.mark.skipif(not (os.path.exists(CHROME) or shutil.which("chromium")), reason="no browser")

# the captcha frame shows on load and is «solved» after 5 s; until then there is no chat input
PAGE = """<!doctype html><meta charset="utf-8">
<iframe id="cap" src="https://challenges.example.com/turnstile/frame" style="width:302px;height:76px"></iframe>
<script>
window.LOADS = (Number(sessionStorage.getItem('loads') || 0) + 1); sessionStorage.setItem('loads', window.LOADS);
setTimeout(() => { document.getElementById('cap').remove();
  document.body.insertAdjacentHTML('beforeend', '<textarea id="box"></textarea><button id="send">Send</button>'); }, 5000);
</script>"""


def test_open_chat_waits_for_the_human_on_a_captcha_without_reloading():
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import Adapter
    from app.config import Settings

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                if "challenges.example.com" in route.request.url:
                    await route.fulfill(status=200, content_type="text/html", body="<p>captcha</p>")
                else:
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=PAGE)

            await context.route("**/*", serve)
            adapter = Adapter(settings=Settings(
                SERVICE_URL="https://service.example.com/", ADAPTER_INPUT_SELECTOR="#box", ADAPTER_SEND_SELECTOR="#send",
                ADAPTER_LOGIN_SELECTOR="#login", ADAPTER_NAVIGATE_TIMEOUT_SECONDS=3))
            events = []

            async def attention(kind, active):
                events.append((kind, active))

            adapter.on_attention = attention
            page = await adapter.open_chat(context, "x")
            loads = await page.evaluate("window.LOADS")
            await browser.close()
            return events, loads

    events, loads = asyncio.run(go())
    assert events == [("captcha", True), ("captcha", False)]
    assert loads == 1  # the page with the captcha was never reloaded
