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


LOGIN_SPA = """
<header><button id="signup">Sign up</button> <button id="login" style="background:#000;color:#fff">Log in</button></header>
<main id="chat" style="display:none"><textarea id="box"></textarea><button id="send">Send</button><div id="sent"></div></main>
<div id="dlg" role="dialog" style="display:none">
  <button id="google">Continue with Google</button>
  <div id="step1"><input id="email" type="email" placeholder="Email"><button type="button" id="cont">Continue</button></div>
  <div id="step2" style="display:none"><input id="pw" type="password"><button type="button" id="go">Log in</button></div>
</div>
<script>
  const $ = (id) => document.getElementById(id);
  window.log = [];
  $("login").onclick = () => { $("dlg").style.display = ""; };
  $("google").onclick = () => window.log.push("google");
  // Enter in the e-mail field does nothing: only the Continue button works
  $("cont").onclick = () => { if ($("email").value) { window.log.push("email:" + $("email").value);
    $("step1").style.display = "none"; $("step2").style.display = ""; } };
  const loggedIn = () => { $("dlg").remove(); $("login").remove(); $("signup").remove(); $("chat").style.display = ""; };
  const tryLogin = () => { if ($("pw").value === "letmein") { localStorage.setItem("in", "1"); loggedIn();
    window.log.push("in"); localStorage.setItem("log", JSON.stringify(window.log)); } else window.log.push("bad"); };
  $("go").onclick = tryLogin;
  $("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") tryLogin(); });
  $("send").onclick = () => { $("sent").textContent = $("box").value; };
  if (localStorage.getItem("in")) { loggedIn(); window.log = JSON.parse(localStorage.getItem("log") || "[]"); }
</script>
"""


def _login_settings(**kw):
    from app.config import Settings

    return Settings(SERVICE_URL="https://service.example.com", ADAPTER_INPUT_SELECTOR="#box",
                    ADAPTER_SEND_SELECTOR="#send", ADAPTER_LOGIN_SELECTOR="#login",
                    ADAPTER_MESSAGE_SELECTOR=".msg", ADAPTER_STATE_SELECTOR=".busy", **kw)


def _run_login(password: str):
    from playwright.async_api import async_playwright

    from app.adapters.generic import adapter as mod

    async def go():
        mod._AUTO_LOGINS.clear()
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page()

            async def serve(route):  # the «service» (a real navigation, like the site)
                await route.fulfill(status=200, content_type="text/html", body=LOGIN_SPA)

            await page.route("https://service.example.com/**", serve)
            await page.goto("https://service.example.com/")
            a = mod.Adapter(settings=_login_settings(ADAPTER_CAPTCHA_WAIT_SECONDS=1))
            a.login_credentials = ("bot.one@example.com", password)
            error = None
            try:
                await a.send_message(page, "hello agent")
            except RuntimeError as exc:
                error = str(exc)
            log = await page.evaluate("window.log")
            sent = await page.evaluate("document.getElementById('sent').textContent")
            await browser.close()
            return log, sent, error

    return asyncio.run(go())


def test_auto_login_in_a_real_page_skips_google_and_sends():
    log, sent, error = _run_login("letmein")
    assert error is None
    assert log == ["email:bot.one@example.com", "in"]  # never clicked «Continue with Google»
    assert sent == "hello agent"


def test_auto_login_with_a_wrong_password_asks_a_human():
    log, sent, error = _run_login("nope")
    assert "google" not in log and "bad" in log
    assert error and "login" in error.lower() and sent == ""


MODEL_BAR = """
<textarea id="box"></textarea><button id="send">Send</button>
<button id="model" aria-haspopup="menu">claude-sonnet-5-5-high</button>
<div id="menu" style="display:none"><div role="menuitem">gemini-4-argon-high</div></div>
<script>
  document.getElementById("model").onclick = () => { document.getElementById("menu").style.display = ""; };
  document.querySelector("[role=menuitem]").onclick = (e) => {
    document.getElementById("model").textContent = e.target.textContent; document.getElementById("menu").remove(); };
</script>
"""


def _model_check(model, clicks="", auto=False):
    from playwright.async_api import async_playwright

    from app.adapters.generic import adapter as mod

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(MODEL_BAR)
            a = mod.Adapter(settings=_login_settings(ADAPTER_MODEL_CLICKS=clicks, ADAPTER_MODEL_AUTO=auto))
            a.expected_model = model
            try:
                await a.prepare_page(page)
                return None
            except RuntimeError as exc:
                return str(exc)
            finally:
                await browser.close()

    return asyncio.run(go())


def test_model_check_passes_when_the_model_is_selected():
    assert _model_check("claude-sonnet-5-5-high") is None


def test_model_check_picks_the_model_with_clicks_or_explains():
    assert _model_check("gemini-4-argon-high",
                        clicks='button[aria-haspopup] || [role="menuitem"]:has-text("{model}")') is None
    error = _model_check("gemini-4-argon-high")  # automatic picking off, no clicks
    assert error and "is not selected" in error and "gemini-4-argon-high" in error
    assert _model_check("gemini-4-argon-high", auto=True) is None  # picked in the model menu
    assert "is not selected" in _model_check("no-such-model-1", auto=True)


TOOLBAR = """
<div id="msgs"><p>Earlier answer mentioning Code and Chat</p></div>
<textarea id="box" placeholder="Describe the website or app you want"></textarea><button id="send">Send</button>
<div class="bar">
  <button>+</button>
  <button id="mode" aria-haspopup="menu">Agent <svg width="8" height="8"></svg></button>
  <button id="modality" aria-haspopup="menu"><svg width="12" height="12"></svg> <svg width="8" height="8"></svg></button>
  <div id="model" style="cursor:pointer;display:inline-block"><svg width="10" height="10"></svg><span> claude-sonnet-5-5-high</span><svg width="8" height="8"></svg></div>
</div>
<div id="layer"></div>
<script>
  const state = { mode: "Agent", modality: "Text", model: "claude-sonnet-5-5-high" };
  window.state = state;
  const layer = document.getElementById("layer");
  const menu = (role, items, onPick) => {
    layer.innerHTML = "";
    const m = document.createElement("div"); m.setAttribute("role", role);
    for (const [title, sub] of items) {
      const it = document.createElement("div"); it.setAttribute("role", role === "menu" ? "menuitem" : "option");
      it.innerHTML = "<div><span>" + title + "</span></div>" + (sub ? "<small>" + sub + "</small>" : "");
      it.onclick = () => { onPick(title); layer.innerHTML = ""; };
      m.append(it);
    }
    layer.append(m); return m;
  };
  document.getElementById("mode").onclick = () => menu("menu", [["Chat", ""], ["Agent", ""]], (t) => {
    state.mode = t; document.getElementById("mode").firstChild.textContent = t + " "; });
  document.getElementById("modality").onclick = () => menu("menu",
    [["Image", "Generate and edit images"], ["Code", "Generate websites and apps"], ["Text", "Text to text chat"]],
    (t) => { state.modality = t; });
  // like the real one: a MULTI-select list (up to 2), «Deselect all», stays open until Escape
  let selected = ["claude-sonnet-5-5-high"];
  const showModel = () => { state.model = selected.join(", ") || "none";
    document.querySelector("#model span").textContent = " " + (selected.length ? selected.join(", ") : "Select model"); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") layer.innerHTML = ""; });
  document.getElementById("model").onclick = () => {
    const items = [["Max", "Router"], ["claude-sonnet-5-5-high", ""], ["gemini-4-argon-high", ""]];
    layer.innerHTML = "";
    const m = document.createElement("div"); m.setAttribute("role", "dialog");
    const search = document.createElement("input"); search.placeholder = "Search models";
    const clear = document.createElement("button"); clear.textContent = "Deselect all";
    clear.onclick = () => { selected = []; state.modality = "Auto"; showModel(); render(); };  // resets Code too
    const list = document.createElement("div");
    const render = () => { list.innerHTML = "";
      for (const [title, sub] of items) {
        if (search.value && !title.includes(search.value)) continue;
        const it = document.createElement("div");  // no role, like many real lists
        it.innerHTML = "<span>" + title + "</span>" + (sub ? " <small>" + sub + "</small>" : "")
          + (selected.includes(title) ? " <svg width=8 height=8></svg>" : "");
        it.onclick = () => { selected = selected.includes(title) ? selected.filter((x) => x !== title)
          : [...selected, title].slice(-2); showModel(); render(); };
        list.append(it);
      } };
    search.oninput = render;
    m.append(search, clear, list); layer.append(m); render();
  };
</script>
"""


def test_chat_toolbar_setup_picks_mode_modality_and_model():
    from playwright.async_api import async_playwright

    from app.adapters.generic import adapter as mod

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(TOOLBAR)
            a = mod.Adapter(settings=_login_settings(ADAPTER_CHAT_MODE="Chat", ADAPTER_MODALITY="Code"))
            a.expected_model = "gemini-4-argon-high"
            await a.prepare_page(page)
            first = await page.evaluate("({...window.state})")
            await a.prepare_page(page)  # again: already set -> nothing breaks
            second = await page.evaluate("({...window.state})")
            await browser.close()
            return first, second

    first, second = asyncio.run(go())
    want = {"mode": "Chat", "modality": "Code", "model": "gemini-4-argon-high"}
    assert first == want and second == want


EMPTY_MODELS_SPA = TOOLBAR.replace("<div id=\"layer\"></div>", """<div id="layer"></div>
<button id="toggle" aria-label="Open sidebar" style="position:fixed;left:4px;top:4px">=</button>
<nav id="side" style="display:none">
  <a id="newchat" href="/">New chat</a>
  <a class="hist" href="/c/0aa1b2c3-d4e5-4f60-8a9b-0c1d2e3f4a5b">Older chat</a>
</nav>""").replace("const state = {", """window.navigated = false;
  const go = (e, path) => { e.preventDefault(); history.pushState({}, "", path); window.navigated = true; };
  document.getElementById("toggle").onclick = () => { document.getElementById("side").style.display = ""; };
  document.querySelector(".hist").onclick = (e) => go(e, "/c/0aa1b2c3-d4e5-4f60-8a9b-0c1d2e3f4a5b");
  document.getElementById("newchat").onclick = (e) => go(e, "/");
  const state = {""").replace(
    '[["Max", "Router"], ["claude-sonnet-5-5-high", ""], ["gemini-4-argon-high", ""]]',
    'window.navigated ? [["Max", "Router"], ["claude-sonnet-5-5-high", ""], ["gemini-4-argon-high", ""]] : []')


def test_empty_model_list_is_fixed_by_opening_a_chat_and_a_new_one_in_the_app():
    from playwright.async_api import async_playwright

    from app.adapters.generic import adapter as mod

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html", body=EMPTY_MODELS_SPA)

            await page.route("https://service.example.com/**", serve)
            await page.goto("https://service.example.com/")
            a = mod.Adapter(settings=_login_settings(ADAPTER_CHAT_MODE="Chat", ADAPTER_MODALITY="Code"))
            a.expected_model = "gemini-4-argon-high"
            await a.prepare_page(page)
            state = await page.evaluate("({...window.state, navigated: window.navigated, path: location.pathname})")
            await browser.close()
            return state

    state = asyncio.run(go())
    assert state == {"mode": "Chat", "modality": "Code", "model": "gemini-4-argon-high", "navigated": True, "path": "/"}
