"""The reply text the adapter rebuilds from a rendered chat message (real
browser): code blocks fenced with only their code, no copy buttons, several
blocks of one message joined, hidden parts skipped."""

from __future__ import annotations

import asyncio
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
