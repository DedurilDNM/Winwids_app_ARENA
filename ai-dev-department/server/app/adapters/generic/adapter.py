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


_CAPTCHA_JS = """
() => {
  // A captcha the HUMAN must solve - not the small reCAPTCHA badge many sites
  // keep in a corner (size=invisible), hidden challenge frames or off-screen ones.
  // Returns a short description (host + size) or null.
  for (const f of document.querySelectorAll("iframe")) {
    const src = f.src || "";
    if (!/captcha|challenge|turnstile/i.test(src)) continue;
    if (/size=invisible/i.test(src)) continue;
    const r = f.getBoundingClientRect();
    const cs = getComputedStyle(f);
    if (r.width < 2 || r.height < 2 || cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) continue;
    if (r.right <= 0 || r.bottom <= 0 || r.left >= innerWidth || r.top >= innerHeight) continue;
    const challenge = /bframe|challenge|turnstile/i.test(src);
    if (!challenge && r.width * r.height < 20000) continue;
    let host = "";
    try { host = new URL(src).host; } catch (e) { host = ""; }
    return `${host || "frame"} ${Math.round(r.width)}x${Math.round(r.height)}`;
  }
  return null;
}
"""

MAX_RESENDS = 5  # resends of one message after page reloads

_BLOCKED_MESSAGES = {
    "captcha": "Service is showing a captcha that did not clear by itself",
    "dialog": "Service is showing a pop-up window that blocks sending",
    "login": "Service requires login: the account session has expired",
}

_MESSAGE_TEXT_JS = r"""
(root) => {
  // Rebuild the message as markdown-ish text: code blocks become fenced blocks
  // with ONLY their code (no language label / copy button), hidden parts and
  // buttons are skipped, block elements end lines.
  const BLOCK = new Set(["P", "DIV", "LI", "UL", "OL", "H1", "H2", "H3", "H4", "H5", "H6", "TABLE", "TR",
                         "BLOCKQUOTE", "SECTION", "ARTICLE", "HEADER", "FOOTER", "DETAILS", "SUMMARY"]);
  const SKIP = new Set(["BUTTON", "SVG", "STYLE", "SCRIPT", "NOSCRIPT", "TEXTAREA", "INPUT"]);
  let out = "";
  const nl = () => { if (out && !out.endsWith("\n")) out += "\n"; };
  const hidden = (el) => { const cs = getComputedStyle(el); return cs.display === "none" || cs.visibility === "hidden"; };
  function walk(n) {
    if (n.nodeType === 3) {  // skip layout indentation, keep spaces between words
      if (n.nodeValue.trim() || !n.nodeValue.includes("\n")) out += n.nodeValue;
      return;
    }
    if (n.nodeType !== 1) return;
    const tag = n.tagName.toUpperCase();
    if (SKIP.has(tag) || n.hasAttribute("data-aidd-skip") || hidden(n)) return;
    if (tag === "PRE") {
      const code = n.querySelector("code") || n;
      const lang = ((code.className || "").match(/language-([\w+#.-]+)/) || [])[1] || "";
      nl();
      out += "FENCE" + lang + "\n" + code.textContent.replace(/\n$/, "") + "\nFENCE\n";
      return;
    }
    if (tag === "BR") { out += "\n"; return; }
    const block = BLOCK.has(tag);
    if (block) nl();
    for (const c of n.childNodes) walk(c);
    if (block) nl();
  }
  // Code-block chrome: short header siblings of a <pre> (language label, copy button).
  const marked = [];
  for (const pre of root.querySelectorAll("pre")) {
    const wrap = pre.parentElement;
    if (!wrap || wrap === root) continue;
    for (const sib of wrap.children) {
      if (sib === pre || sib.contains(pre) || /^(P|H[1-6]|UL|OL|LI|TABLE|BLOCKQUOTE)$/.test(sib.tagName)) continue;
      if (sib.querySelector("pre, p") || (sib.textContent || "").trim().length > 40) continue;
      sib.setAttribute("data-aidd-skip", "1");
      marked.push(sib);
    }
  }
  walk(root);
  for (const m of marked) m.removeAttribute("data-aidd-skip");
  return out.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}
""".replace("FENCE", "```")


_DIALOG_JS = """
() => [...document.querySelectorAll("[role=dialog], [role=alertdialog], [aria-modal=true], dialog[open]")].some((d) => {
  const r = d.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
})
"""

_PASTE_JS = """
(el, text) => {
  el.focus();
  const dt = new DataTransfer();
  dt.setData("text/plain", text);
  el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  return (el.innerText || "").replace(/\\s+/g, "").length;
}
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _alnum(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").lower()


def _is_echo(candidate: str, sent: str) -> bool:
    """True if `candidate` is (a prefix of) the prompt we sent, not a reply.
    Also compares letters/digits only: a chat shown again after a reload may
    render our own message as markdown (other quotes, bullets, spacing)."""
    c, s = _norm(candidate), _norm(sent)
    if not c or not s:
        return False
    if c == s or (len(c) >= 20 and s.startswith(c)):
        return True
    ca, sa = _alnum(c), _alnum(s)
    return len(ca) >= 20 and (ca == sa or sa.startswith(ca))


class Adapter:
    """Selector-driven adapter for a generic chat-based AI service."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()
        self._last_sent: dict[int, str] = {}  # id(page) -> last prompt (echo filter)
        self._before: dict[int, tuple[int, str]] = {}
        self._marks: dict[int, str] = {}  # id(page) -> reload marker
        # async (kind: "captcha" | "dialog" | "login", active: bool) -> None; set by the orchestrator
        self.on_attention = None
        # async () -> bool: False once the project was stopped (ends a pause)
        self.should_continue = None  # id(page) -> chat snapshot taken before sending

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
            text = await last.evaluate(_MESSAGE_TEXT_JS)
        except Exception:
            text = None
        if text is not None:
            return (text or "").strip()
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
            await self._pause(page, "login", self._is_login_required)
            await self._reopen(page)  # logging in may have navigated away from a new chat
        input_sel = self._selectors["input"]
        send_sel = self._selectors["send"]

        # Snapshot BEFORE sending: a fast reply must not end up in "before".
        self._before[id(page)] = (await self._message_count(page), await self._last_message_text(page))
        self._last_sent[id(page)] = text
        await self._submit(page, text)
        logger.debug("Message sent to service (len=%d)", len(text))

    async def _captcha_visible(self, page, on_error: bool = False) -> bool:
        try:
            return bool(await page.evaluate(_CAPTCHA_JS))
        except Exception:  # e.g. the page is navigating (a reload)
            return on_error

    async def _input_pending(self, page) -> bool:
        """True if text is still sitting in the chat input (not sent)."""
        el = await self._first_visible(page, self._selectors["input"])
        if el is None:
            return False
        try:
            return bool(await el.evaluate("(e) => ((e.value !== undefined ? e.value : e.innerText) || '').trim().length"))
        except Exception:
            return False

    async def _page_loading(self, page) -> bool:
        """True while the page is (re)loading or its script context is gone."""
        if not hasattr(page, "evaluate"):
            return False  # minimal page objects (tests)
        try:
            state = await page.evaluate("() => document.readyState")
        except Exception:
            return True
        return state == "loading"

    async def _blocker(self, page, on_error: bool = False) -> str | None:
        """"captcha" | "dialog" (a pop-up while our message is still unsent) | None."""
        if await self._captcha_visible(page, on_error):
            return "captcha"
        try:
            dialog = bool(await page.evaluate(_DIALOG_JS))
        except Exception:
            dialog = False
        if dialog and await self._input_pending(page):
            return "dialog"
        return None

    async def _attention(self, kind: str, active: bool) -> None:
        cb = self.on_attention
        if cb is None:
            return
        try:
            await cb(kind, active)
        except Exception:
            logger.warning("attention callback failed", exc_info=True)

    async def _pause(self, page, kind: str, still_blocked) -> float:
        """PAUSE until a human resolves `kind` ("captcha" | "dialog" | "login")
        in the agent's browser. Tells the user (on_attention), waits without a
        time limit by default (ADAPTER_CAPTCHA_WAIT_SECONDS > 0 sets one), stops
        when the project is stopped (should_continue). Returns seconds paused."""
        limit = float(getattr(self.settings, "ADAPTER_CAPTCHA_WAIT_SECONDS", 0) or 0)
        start = _MONOTONIC()
        detail = ""
        if kind == "captcha":
            try:
                detail = f" [{await page.evaluate(_CAPTCHA_JS)}]"
            except Exception:
                detail = ""
        logger.warning("Paused: %s needs a human%s%s", kind, detail, f" (limit {limit:.0f}s)" if limit else "")
        await self._attention(kind, True)
        try:
            clear = 0
            while clear < 2:  # the page must look clear twice in a row, fully loaded
                if limit and _MONOTONIC() - start > limit:
                    raise RuntimeError(_BLOCKED_MESSAGES.get(kind, f"Service is blocked by: {kind}"))
                if self.should_continue is not None and not await self.should_continue():
                    raise RuntimeError("Stopped by the user while waiting for a human")
                if await self._page_loading(page):
                    clear = 0  # a reload in progress is not "resolved"
                else:
                    try:
                        blocked = await still_blocked(page)
                    except Exception:
                        blocked = True
                    clear = 0 if blocked else clear + 1
                if clear < 2:
                    await asyncio.sleep(2.0)
            logger.info("Resumed: %s resolved after %.0fs", kind, _MONOTONIC() - start)
            return _MONOTONIC() - start
        finally:
            await self._attention(kind, False)

    async def _wait_blockers(self, page) -> bool:
        """After sending: a captcha, or a pop-up while our message is still
        unsent -> pause for the human, then send the message if it is still in
        the input. Never resends a delivered message."""
        await asyncio.sleep(1.0)
        kind = await self._blocker(page)
        if kind is None:
            return False

        async def blocked(p) -> bool:
            return await self._blocker(p, on_error=True) is not None

        await self._pause(page, kind, blocked)
        if await self._reloaded(page):
            return True  # the human reloaded the page: our text is gone, send again
        if await self._input_pending(page):
            try:
                await page.click(self._selectors["send"], timeout=5_000)
            except Exception:
                await page.keyboard.press("Enter")
        return False

    async def _reopen(self, page) -> None:
        template = getattr(self.settings, "ADAPTER_NEW_CHAT_URL", "") or ""
        base = self.settings.SERVICE_URL.rstrip("/")
        try:
            await page.goto(template.format(base=base, chat_ref="") if template else self.settings.SERVICE_URL,
                            wait_until="domcontentloaded",
                            timeout=self._timeout_ms(self.settings.ADAPTER_NAVIGATE_TIMEOUT_SECONDS))
            await self.prepare_page(page)
        except Exception:
            logger.warning("Could not reopen a new chat after the pause", exc_info=True)

    async def after_reply(self, page) -> None:
        """Close what services show after an answer (rating / feedback panels):
        press Escape, and click ADAPTER_DISMISS_SELECTOR if configured."""
        if getattr(self.settings, "ADAPTER_AFTER_REPLY_ESCAPE", True):
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        dismiss = getattr(self.settings, "ADAPTER_DISMISS_SELECTOR", "") or ""
        if dismiss:
            el = await self._first_visible(page, dismiss)
            if el is not None:
                try:
                    await el.click()
                except Exception:
                    pass

    async def _submit(self, page, text: str, depth: int = 0) -> None:
        """Type, mark the page (to notice a reload), send, handle pop-ups /
        captcha. If the human reloaded the page while helping, send again."""
        input_sel, send_sel = self._selectors["input"], self._selectors["send"]
        try:
            await self._write_input(page, text)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Could not focus chat input ({input_sel}): {exc}") from exc
        await self._mark(page)
        try:
            await page.click(send_sel, timeout=10_000)
        except Exception:
            try:
                await page.press(input_sel, "Enter")
            except Exception as exc:
                raise RuntimeError(f"Could not send message (no working send selector: {send_sel})") from exc
        await self._accept_dialog_and_resend(page)
        resend = await self._wait_blockers(page)
        if resend and depth < MAX_RESENDS:
            logger.info("The page was reloaded while a human helped; sending the message again")
            await self._wait_ready(page)
            await self.prepare_page(page)  # a reload resets things like the chat mode
            await self._submit(page, text, depth + 1)

    async def _wait_ready(self, page) -> None:
        """After a reload: wait until the page has loaded and shows the chat input."""
        try:
            await page.wait_for_load_state("load", timeout=30_000)
        except Exception:
            pass
        try:
            await page.wait_for_selector(self._selectors["input"], state="visible", timeout=30_000)
        except Exception:
            logger.warning("The chat input did not appear after the reload")

    async def _mark(self, page) -> None:
        """An invisible marker on the page: gone after a reload."""
        token = f"aidd-{id(page)}-{_MONOTONIC()}"
        try:
            await page.evaluate("(t) => { window.__aiddMark = t; }", token)
            self._marks[id(page)] = token
        except Exception:
            self._marks.pop(id(page), None)

    async def _reloaded(self, page) -> bool:
        token = self._marks.get(id(page))
        if not token:
            return False
        for attempt in range(3):
            try:
                return (await page.evaluate("() => window.__aiddMark || null")) != token
            except Exception:  # the old page is gone mid-navigation: wait for the new one
                if attempt == 2:
                    if getattr(page, "is_closed", None) and page.is_closed():
                        return False
                    return True
                try:
                    await page.wait_for_load_state("load", timeout=15_000)
                except Exception:
                    await asyncio.sleep(1.0)
        return False

    async def _write_input(self, page, text: str) -> None:
        """Put `text` into the first VISIBLE chat input (a selector may also
        match hidden elements). Rich editors (contenteditable, e.g. ProseMirror)
        get a synthetic paste - it keeps line breaks and is fast for long
        prompts; if the editor ignores it, lines are typed with Shift+Enter."""
        input_sel = self._selectors["input"]
        el = await self._first_visible(page, input_sel)
        for _ in range(20):  # a web app may still be drawing its UI (e.g. right after a reload)
            if el is not None:
                break
            await asyncio.sleep(1.0)
            el = await self._first_visible(page, input_sel)
        if el is None:
            raise RuntimeError(f"Could not focus chat input ({input_sel}): no visible element")
        try:
            editable = bool(await el.evaluate("(e) => e.isContentEditable"))
        except Exception:
            editable = False
        if not editable:
            fill = getattr(el, "fill", None)
            if fill is not None:
                await fill(text)
            else:  # minimal page objects (tests)
                await page.fill(input_sel, text)
            return

        expected = len(re.sub(r"\s+", "", text))
        await el.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        got = await el.evaluate(_PASTE_JS, text)
        if got >= expected * 0.95:
            return
        logger.info("Editor ignored the synthetic paste (%d/%d chars); typing line by line", got, expected)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        for i, line in enumerate(text.split("\n")):
            if i:
                await page.keyboard.press("Shift+Enter")
            if line:
                await page.keyboard.insert_text(line)

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
        settle = float(getattr(self.settings, "ADAPTER_SETTLE_SECONDS", 3.0))
        sent = self._last_sent.get(id(page), "")

        # Snapshot of the conversation before we wait, so we can detect a
        # genuinely new assistant message (not an echo of what we sent).
        snapshot = self._before.pop(id(page), None)
        if snapshot is None:  # wait_response without send_message (tests / reuse)
            snapshot = (await self._message_count(page), await self._last_message_text(page))
        before_count, before_text = snapshot
        candidate, candidate_since = "", 0.0
        resends = 0

        while True:
            state = await self.get_state(page)

            if state == "error":
                raise RuntimeError("Service entered an error state")
            if state == "login_required":
                await self._pause(page, "login", self._is_login_required)
                # The reply (if any) was lost with the session: let the caller resend.
                raise RuntimeError("Session restored during the reply; the message must be sent again")
            if await self._captcha_visible(page):
                deadline += await self._pause(page, "captcha", lambda p: self._captcha_visible(p, True))
                continue
            if await self._reloaded(page):
                count, last = await self._message_count(page), await self._last_message_text(page)
                if count > before_count and last and last != before_text and not _is_echo(last, sent):
                    await self._mark(page)  # the reply is there after the reload: keep waiting for it
                elif resends < MAX_RESENDS and sent:
                    resends += 1
                    logger.info("The page was reloaded before a reply; sending the message again (%d)", resends)
                    before_count, before_text = count, last
                    candidate = ""
                    await self._wait_ready(page)
                    await self.prepare_page(page)  # a reload resets things like the chat mode
                    await self._submit(page, sent)
                    deadline = max(deadline, _MONOTONIC() + timeout)
                    continue

            if state == "generating":
                candidate = ""  # still working (agents work in several steps)
            else:
                after_count = await self._message_count(page)
                after_text = await self._last_message_text(page)
                fresh = (
                    after_count > before_count
                    and after_text
                    and after_text != before_text
                    and not _is_echo(after_text, sent)
                )
                if fresh:
                    now = _MONOTONIC()
                    if after_text != candidate:
                        candidate, candidate_since = after_text, now
                    if now - candidate_since >= settle:
                        return candidate
                if _MONOTONIC() >= deadline:
                    if candidate:
                        return candidate
                    return after_text if fresh else ""

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
