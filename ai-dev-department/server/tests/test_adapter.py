"""Phase 4 unit tests: generic adapter (selector-driven, no real service needed).

A `StubPage` simulates a chat DOM so we can verify open_chat / send_message /
wait_response / get_state / is_alive logic without Camoufox or a live AI
service. The adapter itself never names a concrete service - everything is
driven by SERVICE_URL + ADAPTER_* selectors.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.generic.adapter import Adapter, State

DEFAULT_INPUT = "textarea, [contenteditable='true'], input[type='text']"
DEFAULT_SEND = "button[type='submit'], [aria-label*='send' i]"
DEFAULT_MESSAGE = "[role='article'], .message, .chat-message"
DEFAULT_STATE = "[aria-busy='true'], .generating, .loading, .error"
DEFAULT_LOGIN = "input[type='password'], [aria-label*='login' i]"


def make_settings(*, overrides=None) -> SimpleNamespace:
    """Isolated settings object per test (no shared mutable state)."""
    base = dict(
        SERVICE_URL="https://service.example.com",
        ADAPTER_INPUT_SELECTOR=DEFAULT_INPUT,
        ADAPTER_SEND_SELECTOR=DEFAULT_SEND,
        ADAPTER_MESSAGE_SELECTOR=DEFAULT_MESSAGE,
        ADAPTER_STATE_SELECTOR=DEFAULT_STATE,
        ADAPTER_LOGIN_SELECTOR=DEFAULT_LOGIN,
        ADAPTER_READY_SELECTOR="",
        ADAPTER_POLL_INTERVAL_SECONDS=0.01,
        ADAPTER_NAVIGATE_TIMEOUT_SECONDS=30.0,
        ADAPTER_MAX_RETRIES=3,
        ADAPTER_RETRY_BACKOFF_SECONDS=2.0,
    )
    base.update(overrides or {})
    return SimpleNamespace(**base)


def _adapter(**overrides) -> Adapter:
    return Adapter(settings=make_settings(overrides=overrides))


# ---------------------------------------------------------------- stub DOM


class _StubEl:
    def __init__(self, *, visible: bool = True, text: str = "", attrs: dict | None = None) -> None:
        self._visible = visible
        self._text = text
        self._attrs = attrs or {}

    async def is_visible(self) -> bool:
        return self._visible

    async def inner_text(self) -> str:
        return self._text

    async def text_content(self) -> str:
        return self._text

    async def get_attribute(self, attr: str) -> str | None:
        return self._attrs.get(attr)


class StubPage:
    """Fake Playwright page simulating a chat UI."""

    def __init__(
        self,
        *,
        state: State = "idle",
        login_required: bool = False,
        messages: list[str] | None = None,
        auto_respond: str | None = None,
    ) -> None:
        self.state = state
        self.login_required = login_required
        self.messages: list[_StubEl] = [
            _StubEl(text=m) for m in (messages or [])
        ]
        self.url: str | None = None
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.pressed: list[tuple[str, str]] = []
        self.wait_calls: list[str] = []
        self.auto_respond = auto_respond
        self._responded = False

    async def goto(self, url: str, **kwargs: object) -> None:
        self.url = url

    async def fill(self, selector: str, text: str) -> None:
        self.filled[selector] = text
        if self.auto_respond is not None and not self._responded:
            self.state = "generating"
            self._responded = True
            asyncio.create_task(self._auto_respond())

    async def _auto_respond(self) -> None:
        await asyncio.sleep(0.05)
        self.state = "idle"
        self.messages.append(_StubEl(text=self.auto_respond))

    async def click(self, selector: str, timeout: float | None = None) -> None:
        self.clicked.append(selector)
        if not await self.query_selector_all(selector):
            raise Exception(f"no element matches {selector!r}")

    async def press(self, selector: str, key: str) -> None:
        self.pressed.append((selector, key))

    async def wait_for_selector(self, selector: str, timeout: float | None = None) -> None:
        self.wait_calls.append(selector)

    async def query_selector_all(self, selector: str) -> list[_StubEl]:
        sel = selector.lower()
        if any(k in sel for k in ("message", "article", "chat-message")):
            return self.messages
        if any(k in sel for k in ("busy", "generating", "loading", "error")):
            if self.state == "generating" and ("generating" in sel or "busy" in sel):
                return [_StubEl(attrs={"aria-busy": "true"})]
            if self.state == "error" and "error" in sel:
                return [_StubEl(attrs={"class": "error"})]
            return []
        if any(k in sel for k in ("password", "login")):
            if self.login_required:
                return [_StubEl()]
            return []
        if any(k in sel for k in ("textarea", "contenteditable", "input[type='text']")):
            return [_StubEl()]
        if any(k in sel for k in ("button[type='submit']", "send", "отправить")):
            return [_StubEl()]
        return []


# ---------------------------------------------------------------- helpers


class FakeContext:
    def __init__(self, page: StubPage) -> None:
        self._page = page

    async def new_page(self) -> StubPage:
        return self._page


async def _run(coro):
    return await coro


# ---------------------------------------------------------------- tests


def test_adapter_uses_configured_selectors():
    a = _adapter(ADAPTER_INPUT_SELECTOR="#my-input", ADAPTER_SEND_SELECTOR="#my-send")
    assert a._selectors["input"] == "#my-input"
    assert a._selectors["send"] == "#my-send"


def test_open_chat_navigates_to_new_chat_url_and_waits_for_input():
    page = StubPage()
    a = _adapter()
    result = asyncio.run(_run(a.open_chat(FakeContext(page), "project-42")))
    assert result is page
    assert page.url == "https://service.example.com"  # default: SERVICE_URL is a new chat
    assert DEFAULT_INPUT in page.wait_calls

    page2 = StubPage()
    a2 = _adapter(ADAPTER_NEW_CHAT_URL="{base}/new?ref={chat_ref}")
    asyncio.run(_run(a2.open_chat(FakeContext(page2), "project-42")))
    assert page2.url == "https://service.example.com/new?ref=project-42"


def test_setup_clicks_run_in_order_and_failures_are_skipped():
    class ClickPage:
        def __init__(self):
            self.clicked = []

        async def click(self, selector, timeout=None):
            if "missing" in selector:
                raise TimeoutError("not found")
            self.clicked.append(selector)

    page = ClickPage()
    a = _adapter(ADAPTER_SETUP_CLICKS='button[aria-label="Mode"] || .missing || [role="option"]:has-text("Agent")')
    result = asyncio.run(_run(a.prepare_page(page)))
    assert page.clicked == ['button[aria-label="Mode"]', '[role="option"]:has-text("Agent")']
    assert [ok for _, ok in result] == [True, False, True]


def test_send_message_fills_input_and_clicks_send():
    page = StubPage()
    a = _adapter()
    asyncio.run(_run(a.send_message(page, "build the UI")))
    assert page.filled[DEFAULT_INPUT] == "build the UI"
    assert any("send" in c.lower() or "submit" in c.lower() for c in page.clicked)


def test_send_message_falls_back_to_enter():
    page = StubPage()
    # No matching send selector -> should fall back to Enter.
    a = _adapter(ADAPTER_SEND_SELECTOR=".does-not-exist")
    asyncio.run(_run(a.send_message(page, "hello")))
    assert page.filled[DEFAULT_INPUT] == "hello"
    assert any(k == "Enter" for _, k in page.pressed)


def test_send_message_raises_when_login_required():
    page = StubPage(login_required=True)
    a = _adapter(ADAPTER_CAPTCHA_WAIT_SECONDS=0.1)  # with a limit; default = pause until a human logs in
    with pytest.raises(RuntimeError, match="login"):
        asyncio.run(_run(a.send_message(page, "hello")))


def test_wait_response_returns_assistant_message():
    page = StubPage(auto_respond="The UI is ready.")
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01)

    async def _flow() -> str:
        await a.send_message(page, "build the UI")
        return await a.wait_response(page, timeout=10)

    text = asyncio.run(_run(_flow()))
    assert text == "The UI is ready."


def test_wait_response_error_state_raises():
    page = StubPage(state="error")
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01)
    with pytest.raises(RuntimeError, match="error state"):
        asyncio.run(_run(a.wait_response(page, timeout=5)))


def test_wait_response_login_required_raises():
    page = StubPage(state="idle", login_required=True)
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01, ADAPTER_CAPTCHA_WAIT_SECONDS=0.1)
    with pytest.raises(RuntimeError, match="login"):
        asyncio.run(_run(a.wait_response(page, timeout=5)))


def test_wait_response_timeout_raises():
    page = StubPage(state="generating")  # never becomes idle
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01, ADAPTER_MAX_REPLY_SECONDS=0.3)
    with pytest.raises(TimeoutError):
        asyncio.run(_run(a.wait_response(page, timeout=0.2)))


def test_get_state_idle():
    page = StubPage(state="idle")
    a = _adapter()
    assert asyncio.run(_run(a.get_state(page))) == "idle"


def test_get_state_generating():
    page = StubPage(state="generating")
    a = _adapter()
    assert asyncio.run(_run(a.get_state(page))) == "generating"


def test_get_state_error():
    page = StubPage(state="error")
    a = _adapter()
    assert asyncio.run(_run(a.get_state(page))) == "error"


def test_get_state_login_required():
    page = StubPage(state="idle", login_required=True)
    a = _adapter()
    assert asyncio.run(_run(a.get_state(page))) == "login_required"


def test_is_alive_returns_false_on_navigation_error():
    class BoomContext:
        async def new_page(self):
            raise OSError("network down")

    a = _adapter()
    assert asyncio.run(_run(a.is_alive(BoomContext()))) is False


# ---------------------------------------------------------------- end


def test_visible_stop_button_means_generating():
    """A state selector that matches a plain Stop button (no busy attributes)."""

    class El:
        def __init__(self, attrs):
            self.attrs = attrs

        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            return self.attrs.get(name)

    class Page:
        def __init__(self, els):
            self.els = els

        async def query_selector_all(self, sel):
            return self.els

    a = _adapter(ADAPTER_LOGIN_SELECTOR="", ADAPTER_STATE_SELECTOR="button[aria-label*='Stop']")
    assert asyncio.run(a.get_state(Page([El({"aria-label": "Stop generating"})]))) == "generating"
    assert asyncio.run(a.get_state(Page([El({"aria-busy": "false"})]))) == "idle"
    assert asyncio.run(a.get_state(Page([El({"class": "toast error"})]))) == "error"
    assert asyncio.run(a.get_state(Page([]))) == "idle"


def test_wait_response_ignores_echo_and_waits_for_text_to_settle():
    """Hard case: no busy indicator at all; the service first shows OUR prompt,
    then streams the reply in chunks. The adapter must return the full reply."""
    page = StubPage()
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01, ADAPTER_SETTLE_SECONDS=0.2)
    prompt = "Please build the landing page with a hero section and a footer"

    async def service():
        await asyncio.sleep(0.02)
        page.messages.append(_StubEl(text=prompt))  # echo of the user message
        await asyncio.sleep(0.1)
        reply = _StubEl(text="Working")
        page.messages.append(reply)
        for chunk in ("Working on it", "Working on it... done: FILE: a.py"):
            await asyncio.sleep(0.1)
            reply._text = chunk

    async def flow():
        await a.send_message(page, prompt)
        task = asyncio.create_task(service())
        text = await a.wait_response(page, timeout=10)
        await task
        return text

    assert asyncio.run(flow()) == "Working on it... done: FILE: a.py"


def test_is_echo():
    from app.adapters.generic.adapter import _is_echo

    assert _is_echo("Reply with only the word PONG1", "Reply with only the word PONG1")
    assert _is_echo("You are the Coder on the software", "You are the Coder on the software project X\nmore")
    assert not _is_echo("PONG1", "Reply with only the word PONG1")
    assert not _is_echo("ok", "")


def test_captcha_after_send_waits_then_fails_without_resending():
    captcha = {"on": True}

    async def evaluate(script, *args):
        return captcha["on"]

    page = StubPage()
    page.evaluate = evaluate
    a = _adapter(ADAPTER_CAPTCHA_WAIT_SECONDS=2.5)
    with pytest.raises(RuntimeError, match="captcha"):
        asyncio.run(_run(a.send_message(page, "hello")))
    assert page.filled == {DEFAULT_INPUT: "hello"}  # typed once, never resent


def test_captcha_that_clears_by_itself_is_fine():
    captcha = {"on": True}

    async def evaluate(script, *args):
        return captcha["on"]

    page = StubPage()
    page.evaluate = evaluate
    a = _adapter(ADAPTER_CAPTCHA_WAIT_SECONDS=30)

    async def flow():
        async def clear_later():
            await asyncio.sleep(2.5)
            captcha["on"] = False

        task = asyncio.create_task(clear_later())
        await a.send_message(page, "hello")
        await task

    asyncio.run(_run(flow()))


def test_popup_blocking_send_notifies_and_sends_after_user_closes_it():
    """A pop-up stays open and our text is still in the input: the adapter
    asks for a human, waits, and sends once the pop-up is gone."""
    state = {"dialog": True, "pending": True}
    events = []

    class El(_StubEl):
        async def evaluate(self, script, *args):
            if "isContentEditable" in script:
                return False  # a plain textarea
            return 1 if state["pending"] else 0

        async def fill(self, text):
            state["pending"] = True

    page = StubPage()

    async def evaluate(script, *args):
        if "dialog" in script:
            return state["dialog"]
        return False  # no captcha

    async def query(selector):
        if "textarea" in selector or "contenteditable" in selector:
            return [El()]
        return await StubPage.query_selector_all(page, selector)

    async def click(selector, timeout=None):
        page.clicked.append(selector)
        if not state["dialog"]:
            state["pending"] = False  # sending works only once the pop-up is gone

    page.evaluate, page.query_selector_all, page.click = evaluate, query, click
    a = _adapter(ADAPTER_CAPTCHA_WAIT_SECONDS=30)

    async def on_attention(kind, active):
        events.append((kind, active))

    a.on_attention = on_attention

    async def flow():
        async def user_closes_popup():
            await asyncio.sleep(2.5)
            state["dialog"] = False

        task = asyncio.create_task(user_closes_popup())
        await a.send_message(page, "hello")
        await task

    state["pending"] = True
    page.click = click
    asyncio.run(_run(flow()))
    assert events == [("dialog", True), ("dialog", False)]
    assert state["pending"] is False


def test_after_reply_presses_escape_and_clicks_dismiss():
    class Keyboard:
        def __init__(self):
            self.keys = []

        async def press(self, key):
            self.keys.append(key)

    clicked = []

    class Btn(_StubEl):
        async def click(self):
            clicked.append("x")

    page = StubPage()
    page.keyboard = Keyboard()

    async def query(selector):
        return [Btn()] if "close-feedback" in selector else []

    page.query_selector_all = query
    a = _adapter(ADAPTER_DISMISS_SELECTOR="[data-x=close-feedback]")
    asyncio.run(_run(a.after_reply(page)))
    assert page.keyboard.keys == ["Escape"] and clicked == ["x"]



def test_logout_at_send_pauses_until_the_user_logs_in_then_sends():
    page = StubPage(login_required=True)
    events = []
    a = _adapter()  # default: no time limit

    async def on_attention(kind, active):
        events.append((kind, active))

    a.on_attention = on_attention

    async def flow():
        async def user_logs_in():
            await asyncio.sleep(2.5)
            page.login_required = False

        task = asyncio.create_task(user_logs_in())
        await a.send_message(page, "hello")
        await task

    asyncio.run(_run(flow()))
    assert events == [("login", True), ("login", False)]
    assert page.filled[DEFAULT_INPUT] == "hello"  # sent after the pause


def test_pause_ends_when_the_project_is_stopped():
    page = StubPage(login_required=True)
    a = _adapter()
    stopped = {"v": False}

    async def should_continue():
        return not stopped["v"]

    a.should_continue = should_continue

    async def flow():
        async def user_stops():
            await asyncio.sleep(2.5)
            stopped["v"] = True

        task = asyncio.create_task(user_stops())
        try:
            await a.send_message(page, "hello")
        finally:
            await task

    with pytest.raises(RuntimeError, match="Stopped by the user"):
        asyncio.run(_run(flow()))
    assert page.filled == {}  # nothing was sent


def test_captcha_during_the_reply_pauses_and_extends_the_deadline():
    """A captcha shows up while waiting for the reply and stays longer than the
    reply timeout: the task waits for the human instead of timing out."""
    page = StubPage()
    captcha = {"on": False}

    async def evaluate(script, *args):
        return captcha["on"] if "captcha" in script else False

    page.evaluate = evaluate
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.05, ADAPTER_SETTLE_SECONDS=0.1)

    async def flow():
        async def service():
            captcha["on"] = True
            await asyncio.sleep(4.5)          # longer than the 3 s reply timeout
            captcha["on"] = False
            page.messages.append(_StubEl(text="Done after captcha"))

        task = asyncio.create_task(service())
        text = await a.wait_response(page, timeout=3)
        await task
        return text

    assert asyncio.run(_run(flow())) == "Done after captcha"


class ReloadablePage(StubPage):
    """A chat page with a reload button for the 'human': reload() wipes the
    marker and the input (like F5). Captcha and dialogs are flags."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.mark = None
        self.captcha = False
        self.sent: list[str] = []
        self.keyboard = self

    async def evaluate(self, script, *args):
        if "__aiddMark = t" in script:
            self.mark = args[0]
            return None
        if "__aiddMark ||" in script:
            return self.mark
        if "captcha" in script:
            return self.captcha
        return False

    def reload(self):
        self.mark = None
        self.filled.clear()

    async def press(self, *a):
        pass

    async def click(self, selector, timeout=None):
        self.clicked.append(selector)
        if "send" in selector.lower() or "submit" in selector.lower():
            if not self.captcha and self.filled:
                self.sent.append(next(iter(self.filled.values())))


def test_reload_during_a_captcha_sends_the_message_again():
    page = ReloadablePage()
    events = []
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01)

    async def on_attention(kind, active):
        events.append((kind, active))

    a.on_attention = on_attention
    page.captcha = True  # the first send is stopped by a captcha

    async def flow():
        async def human():
            await asyncio.sleep(2.5)
            page.reload()          # «капча бесконечная» -> F5
            page.captcha = False   # after the reload the captcha is gone

        task = asyncio.create_task(human())
        await a.send_message(page, "build the UI")
        await task

    asyncio.run(_run(flow()))
    assert events == [("captcha", True), ("captcha", False)]
    assert page.sent == ["build the UI"]  # delivered once, after the reload


def test_reload_while_waiting_without_a_reply_resends():
    page = ReloadablePage()
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.02, ADAPTER_SETTLE_SECONDS=0.1)

    async def flow():
        await a.send_message(page, "build the UI")
        assert page.sent == ["build the UI"]

        async def human_then_service():
            await asyncio.sleep(0.2)
            page.reload()  # the reply was lost with the reload
            await asyncio.sleep(0.3)
            page.messages.append(_StubEl(text="Done"))

        task = asyncio.create_task(human_then_service())
        text = await a.wait_response(page, timeout=5)
        await task
        return text

    assert asyncio.run(_run(flow())) == "Done"
    assert page.sent == ["build the UI", "build the UI"]


def test_reload_after_the_reply_arrived_does_not_resend():
    page = ReloadablePage()
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.02, ADAPTER_SETTLE_SECONDS=0.3)

    async def flow():
        await a.send_message(page, "build the UI")
        page.messages.append(_StubEl(text="Here it is"))
        page.reload()  # e.g. the user refreshed while watching
        return await a.wait_response(page, timeout=5)

    assert asyncio.run(_run(flow())) == "Here it is"
    assert page.sent == ["build the UI"]


class NavigatingPage(ReloadablePage):
    """A real reload takes time: while the new page loads, scripts fail
    (context destroyed) and then the chat UI appears a bit later."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.navigating = False
        self.hidden_input = False

    async def evaluate(self, script, *args):
        if self.navigating:
            raise RuntimeError("Execution context was destroyed")
        if "readyState" in script:
            return "complete"
        return await super().evaluate(script, *args)

    async def wait_for_load_state(self, *a, **kw):
        while self.navigating:
            await asyncio.sleep(0.05)

    async def wait_for_selector(self, *a, **kw):
        while self.hidden_input:
            await asyncio.sleep(0.05)

    async def query_selector_all(self, selector):
        if self.navigating or (self.hidden_input and "textarea" in selector):
            return []
        return await super().query_selector_all(selector)


def test_slow_reload_during_a_captcha_waits_for_the_page_then_resends():
    page = NavigatingPage()
    events = []
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01)

    async def on_attention(kind, active):
        events.append((kind, active))

    a.on_attention = on_attention
    page.captcha = True

    async def flow():
        async def human():
            await asyncio.sleep(2.5)
            page.navigating = True       # F5: the old page is gone, the new one loads
            page.reload()
            page.captcha = False
            await asyncio.sleep(3.0)     # longer than one pause check
            page.navigating = False
            page.hidden_input = True     # the web app is still drawing its UI
            await asyncio.sleep(1.5)
            page.hidden_input = False

        task = asyncio.create_task(human())
        await a.send_message(page, "build the UI")
        await task

    asyncio.run(_run(flow()))
    assert events == [("captcha", True), ("captcha", False)]
    assert page.sent == ["build the UI"]


def test_reloaded_chat_showing_our_own_message_as_markdown_is_an_echo():
    from app.adapters.generic.adapter import _is_echo

    sent = 'Build the "UI" module:\n- login page\n- settings page'
    assert _is_echo("Build the «UI» module: • login page • settings page", sent)
    assert not _is_echo("FILE: src/ui.js ... STATUS: DONE", sent)


def test_long_reply_that_keeps_growing_is_not_cut_by_the_timeout():
    """`timeout` is about inactivity: an agent that keeps writing for longer
    than it gets the whole reply, not a cut-off part."""
    page = StubPage()
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.02, ADAPTER_SETTLE_SECONDS=0.25)

    async def flow():
        await a.send_message(page, "build it")
        msg = _StubEl(text="part 0")
        page.messages.append(msg)

        async def agent_writes():
            for i in range(1, 12):
                await asyncio.sleep(0.1)
                msg._text = f"{msg._text} part {i}"
            msg._text += " STATUS: DONE"

        task = asyncio.create_task(agent_writes())
        text = await a.wait_response(page, timeout=0.5)  # shorter than the writing
        await task
        return text

    assert asyncio.run(_run(flow())).endswith("part 11 STATUS: DONE")


def test_no_activity_at_all_times_out():
    page = StubPage()
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.02)

    async def flow():
        await a.send_message(page, "build it")
        await a.wait_response(page, timeout=0.3)

    with pytest.raises(TimeoutError, match="no activity"):
        asyncio.run(_run(flow()))


def test_agent_between_steps_is_waited_for_until_its_reply_is_finished():
    page = StubPage()
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.02, ADAPTER_SETTLE_SECONDS=0.1,
                 ADAPTER_INCOMPLETE_SETTLE_SECONDS=5)
    a.reply_complete = lambda t: "STATUS:" in t

    async def flow():
        await a.send_message(page, "build it")
        msg = _StubEl(text="Step 1: reading the repository")
        page.messages.append(msg)

        async def agent():
            await asyncio.sleep(1.0)  # a long pause between steps (no busy indicator)
            msg._text = "FILE: a.txt ... STATUS: DONE"

        task = asyncio.create_task(agent())
        text = await a.wait_response(page, timeout=10)
        await task
        return text

    assert asyncio.run(_run(flow())) == "FILE: a.txt ... STATUS: DONE"


def test_covered_chat_input_is_freed_with_escape():
    """Something covers the editor (a menu / rating panel): the click fails until Escape."""
    state = {"covered": True, "pasted": ""}

    class Editor(_StubEl):
        async def click(self, timeout=None):
            if state["covered"]:
                raise Exception("ElementHandle.click: Timeout 5000ms exceeded")

        async def evaluate(self, script, *args):
            if "isContentEditable" in script:
                return True
            if args:  # the synthetic paste
                state["pasted"] = args[0]
                return len("".join(args[0].split()))
            return None

    page = StubPage()
    editor = Editor()

    class Keyboard:
        async def press(self, key):
            if key == "Escape":
                state["covered"] = False

    page.keyboard = Keyboard()

    async def query(selector):
        if "textarea" in selector or "contenteditable" in selector:
            return [editor]
        return await StubPage.query_selector_all(page, selector)

    page.query_selector_all = query
    a = _adapter()
    asyncio.run(_run(a.send_message(page, "hello world")))
    assert state["pasted"] == "hello world" and not state["covered"]


class LoginFlowPage(StubPage):
    """Logged out: a «Log in» button opens a dialog with an e-mail field; Enter
    shows the password field; the right password logs in."""

    def __init__(self, password="secret-pass"):
        super().__init__(login_required=True)
        self.password = password
        self.stage = "out"  # out -> email -> password -> in
        self.typed: dict[str, str] = {}
        page = self

        class Field(_StubEl):
            def __init__(self, kind):
                super().__init__()
                self.kind = kind

            async def fill(self, text):
                page.typed[self.kind] = text

            async def press(self, key):
                if key != "Enter":
                    return
                if self.kind == "email" and page.typed.get("email"):
                    page.stage = "password"
                elif self.kind == "password":
                    if page.typed.get("password") == page.password:
                        page.stage, page.login_required = "in", False

        class Button(_StubEl):
            async def click(self, timeout=None):
                page.stage = "email"

        self.email_field, self.pw_field, self.button = Field("email"), Field("password"), Button()

    async def query_selector_all(self, selector):
        if "Log in" in selector and self.stage == "out" and "dialog" not in selector:
            return [self.button]
        if 'type="email"' in selector:
            return [self.email_field] if self.stage == "email" else []
        if 'type="password"' in selector:
            return [self.pw_field] if self.stage == "password" else []
        if "password" in selector or "login" in selector.lower():
            return [_StubEl()] if self.login_required else []
        return await super().query_selector_all(selector)


def _login_adapter(**kw):
    return _adapter(ADAPTER_LOGIN_OPEN_SELECTOR='button:has-text("Log in")',
                    ADAPTER_LOGIN_EMAIL_SELECTOR='input[type="email"]',
                    ADAPTER_LOGIN_PASSWORD_SELECTOR='input[type="password"]',
                    ADAPTER_LOGIN_SUBMIT_SELECTOR='button[type="submit"]',
                    ADAPTER_AUTO_LOGIN_PER_HOUR=2, **kw)


def test_auto_login_types_email_then_password_and_then_sends():
    import app.adapters.generic.adapter as mod

    mod._AUTO_LOGINS.clear()
    page = LoginFlowPage()
    a = _login_adapter()
    a.login_credentials = ("bot1@example.com", "secret-pass")
    saved, events = [], []

    async def on_logged_in():
        saved.append(True)

    async def on_attention(kind, active):
        events.append((kind, active))

    a.on_logged_in, a.on_attention = on_logged_in, on_attention
    asyncio.run(_run(a.send_message(page, "hello")))
    assert page.typed == {"email": "bot1@example.com", "password": "secret-pass"}
    assert page.stage == "in" and saved == [True]
    assert events == []  # nobody was disturbed
    assert page.filled[DEFAULT_INPUT] == "hello"


def test_wrong_password_falls_back_to_asking_a_human(caplog):
    import app.adapters.generic.adapter as mod

    mod._AUTO_LOGINS.clear()
    page = LoginFlowPage(password="the-real-one")
    a = _login_adapter(ADAPTER_CAPTCHA_WAIT_SECONDS=0.5)
    a.login_credentials = ("bot1@example.com", "wrong-pass")
    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="login"):
            asyncio.run(_run(a.send_message(page, "hello")))
    logs = caplog.text
    assert "Auto-login failed" in logs
    assert "wrong-pass" not in logs and "bot1@example.com" not in logs  # never logged


def test_auto_login_is_limited_per_hour():
    import app.adapters.generic.adapter as mod

    mod._AUTO_LOGINS.clear()
    a = _login_adapter()
    a.login_credentials = ("bot2@example.com", "x")
    a.settings.ADAPTER_AUTO_LOGIN_PER_HOUR = 1
    mod._AUTO_LOGINS[__import__("hashlib").sha256(b"bot2@example.com").hexdigest()] = [mod._MONOTONIC()]
    assert asyncio.run(_run(a.auto_login(LoginFlowPage()))) is False
