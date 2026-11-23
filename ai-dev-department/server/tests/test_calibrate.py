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


def _page(variant: str, busy: bool = True, hidden_old: bool = False, title: bool = False) -> str:
    user_attrs = {"data": 'data-author="user"', "class": 'class="row"', "plain": ""}[variant]
    bot_attrs = {"data": 'data-author="assistant"', "class": 'class="row"', "plain": ""}[variant]
    body_cls = {"data": "", "class": ' class="answer"', "plain": ""}[variant]
    old = ("<section style='display:none'><div><p>AIDD-CHECK-OLD old prompt</p></div><div><p>QX1 old</p></div></section>"
           if hidden_old else "")
    return """<!doctype html><meta charset="utf-8">
<main><div id="list"></div></main>""" + old + """
<textarea id="box"></textarea><button id="send">Send</button>
<footer><p>Answers may contain mistakes.</p></footer>""" + ("<script>window.TITLE = true;</script>" if title else "") + """
<script>
const list = document.getElementById("list");
const BUSY = """ + ("true" if busy else "false") + """;
document.getElementById("send").onclick = () => {
  const text = document.getElementById("box").value; document.getElementById("box").value = "";
  const u = document.createElement("div"); u.innerHTML = '<div USER><div><p></p></div></div>'.replace("USER", '""" + user_attrs.replace("'", "\\'") + """');
  u.querySelector("p").textContent = text; list.append(u.firstChild);
  if (window.TITLE && !document.getElementById("title")) {
    // the chat page header is created when the chat opens: a NEW element after the messages
    // whose text is our whole first message (cut by CSS only)
    const t = document.createElement("header"); t.id = "title"; t.textContent = text;
    t.style.cssText = "position:fixed;top:0;left:0;width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:#fff";
    document.body.append(t);
  }
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
    assert profile["checks"] == {"clean_start": True, "markers": True, "code_block": True, "status_line": True,
                                 "own_message_found": True}
    assert not reply.startswith("model-x")  # the reply header (model name) is skipped
    assert "STATUS: DONE" in reply and "SUMMARY: made a.txt" in reply and "AIDD-CHECK" not in reply
    assert "generating" in states  # the learned busy indicator works in a normal task


def test_identical_layout_falls_back_to_the_anchor_reader_and_no_busy_indicator():
    profile, reply, states = _calibrate(_page("plain", busy=False, hidden_old=True))
    assert profile["ok"], profile
    assert profile["reader"] == "anchor" and profile["message"] == ""
    assert profile["busy"] == ""
    assert "SUMMARY: made a.txt" in reply and "Answers may contain" not in reply
    assert profile["skip"] and profile["checks"]["clean_start"]
    assert reply.startswith("FILE: a.txt")  # the «model-x» header before the answer is skipped


def test_profile_selectors_override_the_env_ones():
    from app.adapters.generic.adapter import Adapter

    a = Adapter(settings=_settings())
    assert a._selectors["message"] == ".no-such-message"
    a.profile = {"ok": True, "reader": "selector", "message": "div[data-author=\"assistant\"]", "busy": "", "input": ""}
    assert a._selectors["message"] == 'div[data-author="assistant"]' and a._selectors["state"] == ""
    assert a._selectors["input"] == "#box"
    a.profile = {"ok": True, "reader": "anchor", "message": "", "busy": "button.x"}
    assert a._selectors["message"] == "" and a._selectors["state"] == "button.x"


# ------------------------------------------------------------- preflight


class _WS:
    def __init__(self):
        self.events = []

    async def send_to_project(self, project_id, event):
        self.events.append(event)


def _account(i, **kw):
    from types import SimpleNamespace

    base = dict(id=i, name=f"acc-{i}", adapter_profile=None, model=None, login_email=None, service_note="",
                login_password_encrypted=None, storage_state_encrypted=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_preflight_calibrates_new_accounts_checks_calibrated_ones_and_reports_problems():
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import Adapter
    from app.adapters.generic.calibrate import _now_iso
    from app.db import SessionLocal
    from app.services import orchestrator

    fresh = {"default": {"ok": True, "reader": "selector", "message": 'div[data-author="assistant"]', "busy": "",
                         "input": "", "checked_at": _now_iso(), "modality": "default"}}
    new_acc, calibrated, wrong_model, no_browser = (
        _account(901), _account(902, adapter_profile=fresh), _account(903, adapter_profile=fresh, model="no-such-model-9"),
        _account(904))

    async def go():
        from app.db import init_db

        await init_db()
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            contexts = {}
            for acc in (new_acc, calibrated, wrong_model):
                ctx = await browser.new_context()

                async def serve(route):
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=_page("data"))

                await ctx.route("https://service.example.com/**", serve)
                contexts[acc.id] = ctx

            class BM:
                def get_context(self, account_id):
                    return contexts.get(account_id)

            ws = _WS()
            settings = _settings(ADAPTER_MODEL_AUTO=False)
            problems = await orchestrator._preflight(
                [new_acc, calibrated, wrong_model, no_browser], project_id=1, user_id=1, settings=settings,
                session_factory=SessionLocal, browser_manager=BM(), make_adapter=lambda: Adapter(settings=settings), ws=ws)
            await browser.close()
            return problems, ws.events

    problems, events = asyncio.run(go())
    steps = {}
    for e in events:
        steps.setdefault(e["account_id"], []).append(e["status"])
    assert steps[901] == ["checking", "calibrating", "ok"] and new_acc.adapter_profile["default"]["ok"]
    assert steps[902] == ["checking", "ok"]  # fresh calibration: quick check, no test message
    assert steps[903][-1] == "problem" and steps[904][-1] == "problem"
    assert len(problems) == 2
    assert any("no-such-model-9" in p for p in problems) and any("no browser session" in p for p in problems)


def test_old_calibration_is_not_fresh():
    from app.services.orchestrator import _calibration_fresh

    s = _settings(PREFLIGHT_CALIBRATION_MAX_AGE_HOURS=72)
    assert _calibration_fresh({"ok": True, "checked_at": "2099-01-01T00:00:00"}, s)
    assert not _calibration_fresh({"ok": True, "checked_at": "2000-01-01T00:00:00"}, s)
    assert not _calibration_fresh({"ok": False, "checked_at": "2099-01-01T00:00:00"}, s)
    assert not _calibration_fresh(None, s)


def test_chat_title_holding_our_whole_message_is_not_taken_for_it():
    """The header shows the chat title (= our first message, cut by CSS but complete in
    the DOM) and comes AFTER the messages in the page: the bubble must still win."""
    profile, reply, _ = _calibrate(_page("plain", busy=False, title=True))
    assert profile["ok"], profile
    assert profile["reader"] == "anchor" and profile["checks"]["markers"]
    assert reply.startswith("FILE: a.txt") and "SUMMARY: made a.txt" in reply


HISTORY_SPA = """<!doctype html><meta charset="utf-8">
<button id="toggle" aria-label="Open sidebar" style="position:fixed;left:4px;top:4px">=</button>
<nav id="side" style="display:none;margin-top:40px">
  <a id="newchat" href="/">New chat</a>
  <a class="h" data-mode="Agent" href="/c/0aa1b2c3-d4e5-4f60-8a9b-000000000001">Older agent chat</a>
  <a class="h" data-mode="Text" href="/c/0aa1b2c3-d4e5-4f60-8a9b-000000000002">Older text chat</a>
</nav>
<textarea id="box"></textarea><button id="send">Send</button>
<div class="bar"><button id="mode" aria-haspopup="menu">Chat</button>
  <button id="modality" aria-haspopup="menu"><svg width="12" height="12"></svg></button>
  <div id="model" style="cursor:pointer;display:inline-block"><span>Model</span></div></div>
<div id="layer"></div>
<script>
// like the real service: the model list is filled only after a NEW chat is opened in the
// app from a chat in text mode; in an agent-mode chat the modality cannot be changed
const st = { chatMode: "Text", loaded: false, model: "", visits: [], modeSwitchInAgentChat: 0 }; window.st = st;
const layer = document.getElementById("layer");
const go = (e, path) => { e.preventDefault(); history.pushState({}, "", path); };
document.getElementById("toggle").onclick = () => { document.getElementById("side").style.display = ""; };
for (const a of document.querySelectorAll("a.h")) a.onclick = (e) => { go(e, a.getAttribute("href"));
  st.chatMode = a.dataset.mode; st.visits.push(a.dataset.mode);
  document.getElementById("mode").textContent = a.dataset.mode === "Agent" ? "Agent" : "Chat";
  document.getElementById("modality").style.display = a.dataset.mode === "Agent" ? "none" : ""; };
document.getElementById("newchat").onclick = (e) => { go(e, "/"); st.loaded = st.chatMode === "Text";
  document.getElementById("modality").style.display = ""; document.getElementById("mode").textContent = "Chat"; };
const menu = (items, pick, search) => { layer.innerHTML = ""; const m = document.createElement("div");
  m.setAttribute("role", "dialog");
  if (search) { const s = document.createElement("input"); s.placeholder = "Search models"; m.append(s); }
  for (const t of items) { const it = document.createElement("div"); it.innerHTML = "<span>" + t + "</span>";
    it.onclick = () => { pick(t); layer.innerHTML = ""; }; m.append(it); }
  layer.append(m); };
document.getElementById("mode").onclick = () => menu(["Chat", "Agent"], (t) => {
  // like the real service: choosing Chat inside an agent-mode chat starts a new chat at once
  if (st.chatMode === "Agent" && t === "Chat") { st.modeSwitchInAgentChat += 1; history.pushState({}, "", "/");
    st.chatMode = "Chat"; document.getElementById("modality").style.display = ""; }
  document.getElementById("mode").textContent = t; });
document.getElementById("modality").onclick = () => menu(["Text", "Code"], (t) => { st.chatMode = t; });
document.getElementById("model").onclick = () => menu(st.loaded ? ["gemini-4-argon-high", "claude-sonnet-5-5-high"] : [],
  (t) => { st.model = t; document.querySelector("#model span").textContent = t; }, st.loaded);
</script>"""


def test_empty_model_list_tries_older_chats_until_the_models_appear():
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import Adapter

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HISTORY_SPA)

            await context.route("https://service.example.com/**", serve)
            a = Adapter(settings=_settings(ADAPTER_CHAT_MODE="Chat", ADAPTER_MODALITY="Text"))
            a.expected_model = "claude-sonnet-5-5-high"
            page = await a.open_chat(context, "task-1")
            state = await page.evaluate("({...window.st})")
            await browser.close()
            return state

    state = asyncio.run(go())
    assert state["model"] == "claude-sonnet-5-5-high" and state["loaded"]
    assert state["visits"] == ["Agent", "Text"]  # the agent-mode chat was skipped, the text one helped
    assert state["modeSwitchInAgentChat"] == 0  # the mode of an old agent chat is never switched


LOGGED_OUT_SPA = """<!doctype html><meta charset="utf-8">
<header><button id="signup">Sign up</button> <button id="login">Log in</button></header>
<div id="dlg" role="dialog" style="display:none">
  <button>Continue with Google</button>
  <div id="s1"><input id="email" type="email"><button type="button" id="cont">Continue</button></div>
  <div id="s2" style="display:none"><input id="pw" type="password"><button type="button" id="go">Log in</button></div>
</div>
<textarea id="box"></textarea><button id="send">Send</button>
<div id="bar" style="display:none"><button id="mode" aria-haspopup="menu">Chat</button>
  <button id="modality" aria-haspopup="menu"><svg width="12" height="12"></svg></button>
  <div id="model" style="cursor:pointer;display:inline-block"><span>Model</span></div></div>
<div id="layer"></div>
<script>
// logged out: only «Log in» / «Sign up», no chat toolbar (no model list) - until a login
const $ = (id) => document.getElementById(id);
window.st = { logins: 0, model: "" };
const loggedIn = () => { $("login").remove(); $("signup").remove(); $("dlg").remove(); $("bar").style.display = ""; };
if (localStorage.getItem("in")) loggedIn();
else {
  $("login").onclick = () => { $("dlg").style.display = ""; };
  $("cont").onclick = () => { if ($("email").value) { $("s1").style.display = "none"; $("s2").style.display = ""; } };
  const tryLogin = () => { if ($("pw").value === "letmein") { localStorage.setItem("in", "1"); window.st.logins += 1; loggedIn(); } };
  $("go").onclick = tryLogin;
  $("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") tryLogin(); });
}
const layer = $("layer");
$("model").onclick = () => { layer.innerHTML = ""; const m = document.createElement("div"); m.setAttribute("role", "dialog");
  for (const t of ["gemini-4-argon-high", "claude-sonnet-5-5-high"]) { const it = document.createElement("div");
    it.innerHTML = "<span>" + t + "</span>"; it.onclick = () => { window.st.model = t;
      document.querySelector("#model span").textContent = t; layer.innerHTML = ""; }; m.append(it); }
  layer.append(m); };
</script>"""


def _open_logged_out(creds):
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import Adapter

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=LOGGED_OUT_SPA)

            await context.route("https://service.example.com/**", serve)
            a = Adapter(settings=_settings(ADAPTER_LOGIN_SELECTOR="#no-such-login-form", ADAPTER_CAPTCHA_WAIT_SECONDS=2,
                                           ADAPTER_NAVIGATE_TIMEOUT_SECONDS=10))
            a.expected_model = "claude-sonnet-5-5-high"
            a.login_credentials = creds
            error, state = None, None
            try:
                page = await a.open_chat(context, "task-1")
                state = await page.evaluate("({...window.st})")
            except Exception as exc:
                error = exc
            await browser.close()
            return state, error

    return asyncio.run(go())


def test_logged_out_account_logs_in_first_then_picks_the_model():
    from app.adapters.generic import adapter as mod

    mod._AUTO_LOGINS.clear()
    state, error = _open_logged_out(("bot.one@example.com", "letmein"))
    assert error is None, error
    assert state["model"] == "claude-sonnet-5-5-high"  # chosen AFTER the login


def test_logged_out_account_without_a_password_reports_login_not_model():
    from app.adapters.errors import NeedsHumanError
    from app.adapters.generic import adapter as mod

    mod._AUTO_LOGINS.clear()
    state, error = _open_logged_out(None)
    assert isinstance(error, NeedsHumanError) and "login" in str(error).lower()
    assert "is not selected" not in str(error)


# ------------------------------------------------- rate limits and fewer page loads

RATE_LIMITED_THEN_OK = """<!doctype html><meta charset="utf-8"><title>CHAT</title>
<script>
const n = Number(sessionStorage.getItem("loads") || 0) + 1; sessionStorage.setItem("loads", String(n));
window.LOADS = n;
if (n <= 2) { document.title = "Access denied | used Cloudflare to restrict access";
  document.write("<h1>Error 1015</h1><p>You are being rate limited</p>"); }
else { document.write('<textarea id="box"></textarea><button id="send">Send</button><a id="nc" href="/">New chat</a>'); }
</script>"""


def test_rate_limit_page_pauses_everyone_then_continues():
    from playwright.async_api import async_playwright

    from app.adapters.generic import adapter as mod

    async def go():
        mod._RATE.update(until=0.0, level=0)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=RATE_LIMITED_THEN_OK)

            await context.route("https://service.example.com/**", serve)
            a = mod.Adapter(settings=_settings(ADAPTER_RATE_LIMIT_WAIT_SECONDS=0.5))
            t0 = asyncio.get_running_loop().time()
            page = await a.open_chat(context, "task-1")
            took = asyncio.get_running_loop().time() - t0
            loads = await page.evaluate("window.LOADS")
            await browser.close()
            return loads, took

    loads, took = asyncio.run(go())
    assert loads == 3  # two rate-limited loads, waited, then the chat
    assert took >= 1.4  # 0.5 s, then 1 s (the wait grows when it repeats)


def test_page_loads_are_paced_across_accounts():
    from app.adapters.generic import adapter as mod

    async def go():
        mod._RATE.update(until=0.0, level=0, last_nav=0.0)
        a = mod.Adapter(settings=_settings(ADAPTER_MIN_NAV_INTERVAL_SECONDS=0.3))
        b = mod.Adapter(settings=_settings(ADAPTER_MIN_NAV_INTERVAL_SECONDS=0.3))
        loop = asyncio.get_running_loop()
        times = []

        async def nav(adapter):
            await adapter._pace()
            times.append(loop.time())

        await asyncio.gather(nav(a), nav(b), nav(a), nav(b))
        return sorted(times)

    times = asyncio.run(go())
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.28 for g in gaps), gaps


def test_the_account_tab_is_reused_with_the_site_new_chat_button():
    from playwright.async_api import async_playwright

    from app.adapters.generic import adapter as mod

    html = """<!doctype html><meta charset="utf-8"><script>
      window.LOADS = Number(sessionStorage.getItem("l") || 0) + 1; sessionStorage.setItem("l", String(window.LOADS));
    </script><a id="nc" href="/">New chat</a><textarea id="box"></textarea><button id="send">Send</button>
    <script>document.getElementById("nc").onclick = (e) => { e.preventDefault(); window.NEW = (window.NEW || 0) + 1; };</script>"""

    async def go():
        mod._RATE.update(until=0.0, level=0)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

            await context.route("https://service.example.com/**", serve)
            a = mod.Adapter(settings=_settings())
            first = await a.open_chat(context, "task-1")
            await mod.release_page(first)
            second = await mod.Adapter(settings=_settings()).open_chat(context, "task-2")
            result = (first is second, await second.evaluate("window.LOADS"), await second.evaluate("window.NEW || 0"),
                      len(context.pages), first.is_closed())
            await browser.close()
            return result

    same, loads, new_clicks, pages, closed = asyncio.run(go())
    assert same and not closed and pages == 1
    assert loads == 1 and new_clicks == 1  # the second chat: the site's «new chat» button, no page load
