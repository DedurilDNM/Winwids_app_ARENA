"""Generic adapter for the external AI service (Phase 4).

Works with *any* chat-based AI service whose DOM matches the configured
selectors (see `config.Settings.ADAPTER_*`). No concrete AI service is
named here - everything is driven by `SERVICE_URL` + the selectors.

How an account connects to the service:
- The user logs in manually via VNC (Phase 2); the browser's
  `storage_state` (cookies + localStorage + IndexedDB) is captured and
  stored Fernet-encrypted on `Account.storage_state_encrypted`.
- `BrowserManager.launch(account_id, storage_state=...)` relaunches Camoufox
  with that storage state, so the adapter operates in the page *as that
  account*. The service sees the account's session - the server never sees
  the account password.

Contract (`ServiceAdapter` protocol, `app/adapters/base.py`):
- open_chat(context, chat_ref) -> Page : open the service chat
- send_message(page, text) -> None   : type text + send
- wait_response(page, timeout) -> str: wait for the assistant's reply text
- get_state(page) -> idle|generating|error|login_required
- is_alive(context) -> bool          : reachability check
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

State = Literal["idle", "generating", "error", "login_required"]

# Monotonic clock is immune to wall-clock adjustments (NTP, container pause).
_MONOTONIC = time.monotonic


class Adapter:
    """Selector-driven adapter for a generic chat-based AI service."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ config

    @property
    def _selectors(self) -> dict[str, str]:
        s = self.settings
        return {
            "input": s.ADAPTER_INPUT_SELECTOR,
            "send": s.ADAPTER_SEND_SELECTOR,
            "message": s.ADAPTER_MESSAGE_SELECTOR,
            "state": s.ADAPTER_STATE_SELECTOR,
            "login": s.ADAPTER_LOGIN_SELECTOR,
            "ready": s.ADAPTER_READY_SELECTOR,
            "dialog_accept": getattr(s, "ADAPTER_DIALOG_ACCEPT_SELECTOR", ""),
        }

    def _timeout_ms(self, seconds: float) -> int:
        return int(seconds * 1000)

    # ---------------------------------------------------------------- DOM helpers

    async def _first_visible(self, page, selector: str):
        """Return the first visible element matching `selector`, or None."""
        try:
            els = await page.query_selector_all(selector)
        except Exception:
            return None
        for el in els:
            try:
                if await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def _is_login_required(self, page) -> bool:
        login = self._selectors["login"]
        if not login:
            return False
        try:
            return await self._first_visible(page, login) is not None
        except Exception:
            return False

    async def _detect_state(self, page) -> State:
        """Inspect the page for a state indicator.

        ADAPTER_STATE_SELECTOR = "what is visible only while the service is
        busy" (a Stop button, a spinner, an [aria-busy=true] container...).
        A visible match means "generating", unless its attributes say error.
        """
        state_sel = self._selectors["state"]
        if state_sel:
            try:
                el = await self._first_visible(page, state_sel)
                if el is not None:
                    for attr in ("data-state", "aria-busy", "data-status", "class"):
                        try:
                            value = (await el.get_attribute(attr)) or ""
                        except Exception:
                            value = ""
                        vl = value.lower()
                        if "error" in vl or "failed" in vl:
                            return "error"
                        if attr == "aria-busy" and vl == "false":
                            return "idle"
                    return "generating"
            except Exception:
                pass
        return "idle"

    async def _last_message_text(self, page) -> str:
        """Return the text of the last visible message, or ""."""
        msg_sel = self._selectors["message"]
        if not msg_sel:
            return ""
        try:
            els = await page.query_selector_all(msg_sel)
        except Exception:
            return ""
        if not els:
            return ""
        last = els[-1]
        try:
            text = await last.inner_text()
        except Exception:
            try:
                text = await last.text_content()
            except Exception:
                return ""
        return (text or "").strip()

    async def _message_count(self, page) -> int:
        msg_sel = self._selectors["message"]
        if not msg_sel:
            return 0
        try:
            return len(await page.query_selector_all(msg_sel))
        except Exception:
            return 0

    # ---------------------------------------------------------------- protocol

    async def open_chat(self, context, chat_ref: str):
        """Open the service chat in a new page and wait until ready."""
        page = await context.new_page()
        # Most chat services start a new conversation on their home page.
        # ADAPTER_NEW_CHAT_URL may override it ({base} and {chat_ref} placeholders).
        template = getattr(self.settings, "ADAPTER_NEW_CHAT_URL", "") or ""
        base = self.settings.SERVICE_URL.rstrip("/")
        url = template.format(base=base, chat_ref=chat_ref) if template else self.settings.SERVICE_URL
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self._timeout_ms(self.settings.ADAPTER_NAVIGATE_TIMEOUT_SECONDS),
        )
        ready_sel = self._selectors["ready"]
        wait_sel = ready_sel or self._selectors["input"]
        if wait_sel:
            await page.wait_for_selector(
                wait_sel, timeout=self._timeout_ms(self.settings.ADAPTER_NAVIGATE_TIMEOUT_SECONDS)
            )
        await self.prepare_page(page)
        return page

    async def prepare_page(self, page) -> list[tuple[str, bool]]:
        """Run ADAPTER_SETUP_CLICKS (e.g. switch the chat to a specific mode).

        Returns [(selector, clicked)]. A click that fails is logged and skipped:
        the chat may already be in the wanted state.
        """
        raw = getattr(self.settings, "ADAPTER_SETUP_CLICKS", "") or ""
        results: list[tuple[str, bool]] = []
        for selector in [part.strip() for part in raw.split("||") if part.strip()]:
            try:
                await page.click(selector, timeout=5_000)
                results.append((selector, True))
                await asyncio.sleep(0.5)
            except Exception:
                logger.warning("Setup click not possible: %s", selector)
                results.append((selector, False))
        return results

    async def send_message(self, page, text: str) -> None:
        """Type `text` into the chat input and send it."""
        if await self._is_login_required(page):
            raise RuntimeError("Service requires login: account storage_state may be expired")
        input_sel = self._selectors["input"]
        send_sel = self._selectors["send"]

        try:
            await page.fill(input_sel, text)
        except Exception as exc:
            raise RuntimeError(f"Could not focus chat input ({input_sel}): {exc}") from exc

        sent = False
        try:
            await page.click(send_sel, timeout=10_000)
            sent = True
        except Exception:
            sent = False
        if not sent:
            try:
                await page.press(input_sel, "Enter")
            except Exception as exc:
                raise RuntimeError(
                    f"Could not send message (no working send selector: {send_sel})"
                ) from exc
        await self._accept_dialog_and_resend(page)
        logger.debug("Message sent to service (len=%d)", len(text))

    async def _accept_dialog_and_resend(self, page) -> None:
        """If a configured pop-up button appeared after sending, click it and send again."""
        accept_sel = self._selectors["dialog_accept"]
        if not accept_sel:
            return
        await asyncio.sleep(1.5)
        button = await self._first_visible(page, accept_sel)
        if button is None:
            return
        logger.info("Service pop-up detected after send: clicking the configured accept button")
        await button.click()
        await asyncio.sleep(1.0)
        box = await self._first_visible(page, self._selectors["input"])
        if box is None:
            return
        try:
            pending = await box.evaluate("(el) => ((el.value !== undefined ? el.value : el.innerText) || '').trim()")
        except Exception:
            pending = ""
        if pending:  # the message was not sent because of the pop-up
            try:
                await page.click(self._selectors["send"], timeout=10_000)
            except Exception:
                await page.press(self._selectors["input"], "Enter")

    async def wait_response(self, page, timeout: int = 600) -> str:
        """Wait for the assistant's response and return its text.

        Polls `get_state` until it stops being "generating", then returns the
        last message text. Raises TimeoutError if no response arrives.
        """
        deadline = _MONOTONIC() + timeout
        poll = self.settings.ADAPTER_POLL_INTERVAL_SECONDS

        # Snapshot of the conversation before we wait, so we can detect a
        # genuinely new assistant message (not an echo of what we sent).
        before_count = await self._message_count(page)
        before_text = await self._last_message_text(page)

        while True:
            state = await self.get_state(page)

            if state == "error":
                raise RuntimeError("Service entered an error state")
            if state == "login_required":
                raise RuntimeError("Service requires login during task execution")

            if state == "idle":
                after_count = await self._message_count(page)
                after_text = await self._last_message_text(page)
                if after_count > before_count and after_text and after_text != before_text:
                    return after_text
                # A new message may still be rendering; give it one poll cycle.
                if _MONOTONIC() >= deadline:
                    return after_text if after_text and after_text != before_text else ""

            if _MONOTONIC() >= deadline:
                raise TimeoutError(f"No response from service within {timeout}s")

            await asyncio.sleep(poll)

    async def get_state(self, page) -> State:
        """Current service/page state: idle | generating | error | login_required."""
        if await self._is_login_required(page):
            return "login_required"
        return await self._detect_state(page)

    async def is_alive(self, context) -> bool:
        """True if SERVICE_URL is reachable in the given browser context."""
        page = None
        try:
            page = await context.new_page()
            response = await page.goto(
                self.settings.SERVICE_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            return response is None or response.status < 500
        except Exception:
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass


adapter = Adapter()
