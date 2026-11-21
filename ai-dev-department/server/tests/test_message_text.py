"""The reply text the adapter rebuilds from a rendered chat message (real
browser): code blocks fenced with only their code, no copy buttons, several
blocks of one message joined, hidden parts skipped."""

from __future__ import annotations

import asyncio
import re
import os
import shutil

import pytest

from app.adapters.generic.adapter import _MESSAGE_TEXT_JS
from app.services.agent_output import parse_agent_output

CHROME = os.environ.get("E2E_CHROMIUM") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
pytestmark = pytest.mark.skipif(not (os.path.exists(CHROME) or shutil.which("chromium")), reason="no browser")

MESSAGE_HTML = """
<div id="chat-message-1">
  <div class="thinking" style="display:none">STATUS: DONE (hidden draft, must be ignored)</div>
  <div class="prose"><p>Here are the files.</p><p>FILE: <code>src/app.py</code></p></div>
  <div class="code-block">
    <div class="header"><span>python</span><button>Copy</button></div>
    <pre><code class="language-python">def add(a, b):
    return a + b
</code></pre>
  </div>
  <div class="prose"><p>FILE: README.md</p></div>
  <pre><code>Run: python app.py</code></pre>
  <div class="prose"><p>MESSAGE to Coder: use add()</p><p>STATUS: DONE</p><p>SUMMARY: added <b>the</b> <i>app</i></p></div>
  <button>👍</button>
</div>
"""


def test_rendered_message_becomes_a_parsable_reply():
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(MESSAGE_HTML)
            text = await page.eval_on_selector("#chat-message-1", _MESSAGE_TEXT_JS)
            await browser.close()
            return text

    text = asyncio.run(go())
    assert "Copy" not in text and "👍" not in text and "hidden draft" not in text
    out = parse_agent_output(text)
    assert out.status == "DONE" and out.summary == "added the app"
    assert out.files == {"src/app.py": "def add(a, b):\n    return a + b\n", "README.md": "Run: python app.py\n"}
    assert out.messages == [("Coder", "use add()")]


CAPTCHA_HTML = """
<style>body{margin:0}</style>
<iframe id="badge" style="position:fixed;right:0;bottom:0;width:256px;height:60px"
  src="about:blank#https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"></iframe>
<iframe id="hidden-challenge" style="position:absolute;top:-10000px;width:400px;height:580px"
  src="about:blank#https://www.google.com/recaptcha/api2/bframe?k=x"></iframe>
<iframe id="logo" style="width:60px;height:60px" src="about:blank#https://captcha.example/logo"></iframe>
"""


def _captcha(html_extra: str = ""):
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import _CAPTCHA_JS

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(CAPTCHA_HTML + html_extra)
            result = await page.evaluate(_CAPTCHA_JS)
            await browser.close()
            return result

    return asyncio.run(go())


def test_recaptcha_badge_and_hidden_frames_are_not_a_captcha():
    # Sites keep these on every page: treating them as a captcha froze the agent forever.
    assert _captcha() is None


def test_real_challenges_are_a_captcha():
    shown = '<iframe style="width:400px;height:580px" src="about:blank#https://www.google.com/recaptcha/api2/bframe?k=x"></iframe>'
    assert re.search(r"\d+x\d+", _captcha(shown) or "")
    checkbox = '<iframe style="width:304px;height:78px" src="about:blank#https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>'
    assert _captcha(checkbox)
    turnstile = '<iframe style="width:300px;height:65px" src="about:blank#https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"></iframe>'
    assert _captcha(turnstile)
