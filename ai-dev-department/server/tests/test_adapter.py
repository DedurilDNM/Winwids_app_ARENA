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


def test_open_chat_navigates_to_chat_url_and_waits_for_input():
    page = StubPage()
    ctx = FakeContext(page)
    a = _adapter()
    result = asyncio.run(_run(a.open_chat(ctx, "project-42")))
    assert result is page
    assert page.url == "https://service.example.com/chat?ref=project-42"
    assert DEFAULT_INPUT in page.wait_calls


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
    a = _adapter()
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
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01)
    with pytest.raises(RuntimeError, match="login"):
        asyncio.run(_run(a.wait_response(page, timeout=5)))


def test_wait_response_timeout_raises():
    page = StubPage(state="generating")  # never becomes idle
    a = _adapter(ADAPTER_POLL_INTERVAL_SECONDS=0.01)
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
