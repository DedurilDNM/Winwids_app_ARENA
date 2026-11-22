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
import hashlib
import logging
import re
import time
import weakref
from urllib.parse import urlsplit
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
_AUTO_LOGINS: dict[str, list[float]] = {}  # sha256(email) -> recent auto-login attempt times

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


# Reply without a message selector match (e.g. another chat mode renders other
# markup): anchor on OUR sent message (its first words) and take the blocks right
# after it as the reply. Elements present before sending are marked and ignored,
# so a footer or a hint below the chat is never taken for a reply.
_REPLY_FALLBACK_JS = r"""
(args) => {
  const msgText = __MESSAGE_TEXT__;
  if (args.mark) { document.querySelectorAll('body *').forEach((e) => e.setAttribute('data-aidd-seen', '')); return null; }
  const norm = (t) => (t || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9\u0430-\u044f\u0451]/g, '');
  const head = norm(args.head).slice(0, 80);
  if (head.length < 12) return { count: 0, text: '' };
  const skip = (e) => e.closest('textarea, input, [contenteditable="true"], script, style');
  const hits = [...document.querySelectorAll('body *')].filter((e) => !skip(e) && norm(e.textContent).includes(head));
  const shown = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; };
  // a site may keep an older chat (with the same prompt) hidden in the page: only a VISIBLE
  // copy counts, and one that appeared after we sent (not marked «seen») wins
  const deep = hits.filter((e) => ![...e.children].some((c) => norm(c.textContent).includes(head)) && shown(e));
  if (!deep.length) return { count: 0, text: '' };
  const fresh = deep.filter((e) => !e.hasAttribute('data-aidd-seen'));
  const bubble = (fresh.length ? fresh : deep)[(fresh.length ? fresh : deep).length - 1];
  const top = bubble.getBoundingClientRect().top;
  let row = bubble;
  for (let up = 0; up < 14 && row.parentElement && row.parentElement !== document.body; up++) {
    const parent = row.parentElement;
    const sibs = [...parent.children];
    const after = sibs.slice(sibs.indexOf(row) + 1).filter((x) => !x.hasAttribute('data-aidd-seen')
      && !x.querySelector('textarea, input, [contenteditable="true"]') && !norm(x.textContent).includes(head)
      && (x.innerText || '').trim().length > 0 && shown(x) && x.getBoundingClientRect().top >= top);
    if (after.length) {
      return { count: after.length, text: after.map((x) => msgText(x)).filter(Boolean).join('\n\n').trim() };
    }
    row = parent;
  }
  return { count: 0, text: '' };
}
""".replace("__MESSAGE_TEXT__", _MESSAGE_TEXT_JS.strip())

# Chat setup (mode / modality / model) by visible labels. Marks the element to
# click with data-aidd-pick; returns a boolean only (no page text leaves the page).
_PICK_TOGGLE_JS = r"""
(args) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = (e) => (e.innerText || '').replace(/[⌄˅▾▼⌵∨]/g, '').trim();
  // compare by letters and digits only: other hyphens / spaces / line breaks do not matter
  const norm = (t) => (t || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
  document.querySelectorAll('[data-aidd-pick]').forEach((e) => e.removeAttribute('data-aidd-pick'));
  const btns = [...document.querySelectorAll('button, [role="button"], [role="combobox"], [aria-haspopup]')]
    .filter(vis).filter((b) => !b.closest('[role="menu"], [role="listbox"], [role="dialog"]'));
  const modes = (args.modes || []).map(norm);
  let el = null;
  if (args.kind === 'mode') el = btns.find((b) => modes.includes(norm(label(b))));
  if (args.kind === 'modality') {
    const m = btns.find((b) => modes.includes(norm(label(b))));
    if (m) el = btns.slice(btns.indexOf(m) + 1).find((b) => !m.contains(b) && !b.contains(m) && norm(label(b)).length <= 3);
  }
  const clickable = (t) => {
    let c = t;
    while (c && c !== document.body && !(c.matches('button, [role="button"], [role="combobox"], [aria-haspopup]')
           || getComputedStyle(c).cursor === 'pointer')) c = c.parentElement;
    return c && c !== document.body ? c : t;
  };
  const byText = (test) => {
    const found = [...document.querySelectorAll('body *')].filter((e) => vis(e)
      && !e.closest('[role="menu"], [role="listbox"], [role="dialog"], textarea, [contenteditable="true"], [data-aidd-pick="option"]')
      && test(label(e)) && ![...e.children].some((c) => test(label(c))));
    found.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    return found.length ? clickable(found[0]) : null;
  };
  if (args.kind === 'model') {
    const want = norm(args.model);
    const marked = document.querySelector('[data-aidd-model-btn]');
    el = (marked && vis(marked) ? marked : null)   // found earlier (by its behaviour or its text)
      || btns.find((b) => norm(label(b)) === want)
      || byText((t) => norm(t) === want)
      || btns.find((b) => /^[A-Za-z0-9][\w.]*(-[\w.]+)+$/.test(label(b)) || label(b) === 'Max')
      // several models selected: «a-b-c +1», «2 models», «a-b-c, d-e-f»
      || btns.find((b) => /(^|[\s,])[A-Za-z0-9][\w.]*(-[\w.]+){2,}/.test(label(b)) && label(b).length < 120)
      || btns.find((b) => /^\d+\s+models?$/i.test(label(b)) || /^(select|choose) (a )?models?$/i.test(label(b)));
    if (el && !marked) el.setAttribute('data-aidd-model-btn', '');
  }
  if (!el) return false;
  el.setAttribute('data-aidd-pick', 'toggle');
  return args.current && norm(label(el)) === norm(args.current) ? 'same' : true;
}
"""

# The model chip, found by what it DOES: in the toolbar row of the mode button
# (or of the chat input), every clickable is a candidate, right to left; the one
# that opens a model search box is the chip. Marks data-aidd-cand=<i>, returns count.
_TOOLBAR_CANDIDATES_JS = r"""
(args) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (t) => (t || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
  const isClick = (e) => e.matches('button, [role="button"], [role="combobox"], [aria-haspopup], a[href]')
    || getComputedStyle(e).cursor === 'pointer';
  document.querySelectorAll('[data-aidd-cand]').forEach((e) => e.removeAttribute('data-aidd-cand'));
  const modes = (args.modes || []).map(norm);
  let anchor = [...document.querySelectorAll('button, [role="button"]')].filter(vis)
    .find((b) => modes.includes(norm(b.innerText)));
  if (!anchor && args.input) anchor = [...document.querySelectorAll(args.input)].find(vis) || null;
  if (!anchor) return 0;
  const inPopup = (e) => e.closest('[role="menu"], [role="listbox"], [role="dialog"]');
  let box = anchor.parentElement, cands = [];
  for (let up = 0; box && box !== document.body && up < 7; up++, box = box.parentElement) {
    cands = [...box.querySelectorAll('*')].filter((e) => vis(e) && isClick(e) && !inPopup(e)
      && !e.contains(anchor) && !anchor.contains(e) && !e.closest('textarea, [contenteditable="true"]')
      && e.getBoundingClientRect().width < 420 && e.getBoundingClientRect().height < 90
      && norm(e.innerText).length >= 3);  // the chip shows a name; icon-only buttons (attach, modality) are skipped
    if (cands.length >= 2) break;
  }
  if (args.send) { try { cands = cands.filter((e) => !e.matches(args.send)); } catch (err) {} }
  cands = cands.filter((e) => !cands.some((o) => o !== e && e.contains(o) === false && o.contains(e)));  // outermost
  cands.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
  cands.slice(0, 8).forEach((e, i) => e.setAttribute('data-aidd-cand', String(i)));
  return Math.min(cands.length, 8);
}
"""

# Structure only (no text): what the element showing a model-like name looks like.
_MODEL_DIAG_JS = r"""
() => {
  const shape = (t) => (t || '').trim().slice(0, 40).replace(/[a-z]/g, 'a').replace(/[A-Z]/g, 'A')
    .replace(/[0-9]/g, '9').replace(/[^\x20-\x7e]/g, (c) => 'U+' + c.charCodeAt(0).toString(16));
  const desc = (e) => [e.tagName.toLowerCase(), e.getAttribute('role') || '-', e.hasAttribute('aria-haspopup') ? 'popup' : '-',
    getComputedStyle(e).cursor, JSON.stringify(shape(e.innerText))].join('/');
  const c = [...document.querySelectorAll('[data-aidd-cand]')];
  const m = document.querySelector('[data-aidd-model-btn]');
  return `candidates=${c.length}` + c.map((e) => ` [${e.getAttribute('data-aidd-cand')}: ${desc(e)}]`).join('')
    + (m ? ` chip=[${desc(m)}]` : ' chip=none')
    + ` search=${!!document.querySelector('input[placeholder*="earch" i]')}`;
}
"""

_PICK_OPTION_JS = r"""
(text) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (t) => (t || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
  const want = norm(text);
  const first = (e) => ((e.innerText || '').trim().split('\n')[0] || '').trim();
  const match = (e) => norm(first(e)) === want || norm(e.innerText) === want;
  document.querySelectorAll('[data-aidd-pick="option"]').forEach((e) => e.removeAttribute('data-aidd-pick'));
  const all = [...document.querySelectorAll('body *')].filter((e) => !e.closest('[data-aidd-pick="toggle"]')
    && !e.closest('textarea, [contenteditable="true"], input') && vis(e) && match(e));
  if (!all.length) return false;
  const popup = all.filter((e) => e.closest('[role="menu"], [role="listbox"], [role="dialog"], [data-radix-popper-content-wrapper], [cmdk-list]'));
  const pool = popup.length ? popup : all;
  let best = pool[pool.length - 1];
  for (const e of pool) if (best.contains(e)) best = e;  // the deepest match
  const target = best.closest('[role="menuitem"], [role="menuitemradio"], [role="option"], [cmdk-item], button, li, a') || best;
  target.setAttribute('data-aidd-pick', 'option');
  return true;
}
"""

# In-app navigation (no page reload): the sidebar toggle, the most recent chat in
# the history list, the «new chat» control. Marks the target with data-aidd-nav.
_NAV_JS = r"""
(args) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const meta = (e) => [e.getAttribute('aria-label'), e.getAttribute('title'), e.getAttribute('data-testid')].join(' ');
  document.querySelectorAll('[data-aidd-nav]').forEach((e) => e.removeAttribute('data-aidd-nav'));
  let el = null;
  if (args.selector) {
    el = [...document.querySelectorAll(args.selector)].find(vis) || null;
  } else if (args.kind === 'history') {
    el = [...document.querySelectorAll('a[href]')].filter(vis).find((a) => {
      try {
        const u = new URL(a.href, location.href);
        return u.origin === location.origin && u.pathname !== location.pathname
          && /[0-9a-f]{8}-[0-9a-f]{4}|[0-9a-z_-]{16,}/i.test(u.pathname.split('/').pop() || '');
      } catch (e) { return false; }
    }) || null;
  } else if (args.kind === 'sidebar') {
    const btns = [...document.querySelectorAll('button, [role="button"]')].filter(vis);
    el = btns.find((b) => /sidebar|side bar|history|menu|panel|navigation/i.test(meta(b)))
      || btns.filter((b) => { const r = b.getBoundingClientRect(); return r.left < 90 && r.top < 90 && !(b.innerText || '').trim(); })[0]
      || null;
  } else if (args.kind === 'new') {
    const c = [...document.querySelectorAll('a, button, [role="button"]')].filter(vis);
    el = c.find((b) => /new (chat|thread|conversation|task)/i.test(meta(b) + ' ' + (b.innerText || '')))
      || c.find((b) => { try { return b.tagName === 'A' && new URL(b.href, location.href).pathname === '/'; } catch (e) { return false; } })
      || null;
  }
  if (!el) return false;
  el.setAttribute('data-aidd-nav', '1');
  return true;
}
"""

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
        # Per-page state, keyed by the page OBJECT (weakly): never by id(page) - a new
        # page can get the id of a closed one and inherit its prompt.
        self._last_sent: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # page -> last prompt
        self._before: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # page -> (count, text) before sending
        # Optional: reply text -> bool "looks finished" (set per task by the orchestrator).
        self.reply_complete = None
        # Optional auto-login: (email, password) of this account, and an async
        # callback run after a successful auto-login (saves the session).
        self.login_credentials: tuple[str, str] | None = None
        # Optional: the model name the account's chat must use (Account.model).
        self.expected_model: str | None = None
        self.on_logged_in = None
        self._marks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # page -> reload marker
        # async (kind: "captcha" | "dialog" | "login", active: bool) -> None; set by the orchestrator
        self.on_attention = None
        # async () -> bool: False once the project was stopped (ends a pause)
        self.should_continue = None

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
            fallback = await self._fallback_reply(page)
            return fallback[1] if fallback else ""
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

    async def _selector_count(self, page) -> int:
        msg_sel = self._selectors["message"]
        if not msg_sel:
            return 0
        try:
            return len(await page.query_selector_all(msg_sel))
        except Exception:
            return 0

    async def _message_count(self, page) -> int:
        count = await self._selector_count(page)
        if count == 0:
            fallback = await self._fallback_reply(page)
            return fallback[0] if fallback else 0
        return count

    async def _fallback_reply(self, page) -> tuple[int, str] | None:
        """(count, text) found next to our own last sent message, or None."""
        sent = self._last_sent.get(page, "")
        if not sent or not getattr(self.settings, "ADAPTER_REPLY_FALLBACK", True):
            return None
        try:
            result = await page.evaluate(_REPLY_FALLBACK_JS, {"head": sent[:400]})
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        return int(result.get("count") or 0), str(result.get("text") or "")

    async def _mark_seen(self, page) -> None:
        if not hasattr(page, "evaluate"):
            return
        try:
            await page.evaluate(_REPLY_FALLBACK_JS, {"mark": True})
        except Exception:
            pass

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
        try:
            await self._setup_chat(page)
        except Exception:
            logger.warning("Chat setup failed", exc_info=True)
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
        await self._ensure_model(page)
        return results

    def _model_selector(self, model: str) -> str:
        m = model.replace("\\", "").replace('"', "")
        return (f'button:has-text("{m}"), [role="button"]:has-text("{m}"), [role="combobox"]:has-text("{m}"), '
                f'[aria-haspopup]:has-text("{m}")')

    async def _pick(self, page, kind: str, **args) -> bool | str:
        try:
            return await page.evaluate(_PICK_TOGGLE_JS, {"kind": kind, **args})
        except Exception:
            return False

    async def _choose(self, page, text: str, search: bool = False) -> bool:
        """With a menu open: (optionally type `text` into its search box) click the option labelled `text`."""
        if search:
            box = await self._first_visible(page, 'input[placeholder*="earch" i], input[type="search"]')
            if box is not None:
                try:
                    await box.fill(text)
                    await asyncio.sleep(0.6)
                except Exception:
                    pass
        for _ in range(6):
            try:
                if await page.evaluate(_PICK_OPTION_JS, text):
                    await page.click('[data-aidd-pick="option"]', timeout=5_000)
                    await asyncio.sleep(0.6)
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    async def _setup_chat(self, page) -> None:
        """ADAPTER_CHAT_MODE (e.g. Chat) and ADAPTER_MODALITY (e.g. Code): pick them
        in the chat toolbar by their visible labels. Failures are logged only."""
        s = self.settings
        modes = [m.strip() for m in (getattr(s, "ADAPTER_CHAT_MODES", "") or "").split(",") if m.strip()]
        mode = (getattr(s, "ADAPTER_CHAT_MODE", "") or "").strip()
        if mode:
            found = await self._pick(page, "mode", modes=modes + [mode], current=mode)
            if found is True:
                await page.click('[data-aidd-pick="toggle"]', timeout=5_000)
                await asyncio.sleep(0.5)
                if not await self._choose(page, mode):
                    logger.warning("Chat mode %s: option not found", mode)
                    await page.keyboard.press("Escape")
            elif not found:
                logger.warning("Chat mode %s: mode button not found", mode)
        await self._set_modality(page)

    async def _set_modality(self, page) -> bool:
        """ADAPTER_MODALITY (e.g. Code) in the menu next to the mode button."""
        s = self.settings
        modality = (getattr(s, "ADAPTER_MODALITY", "") or "").strip()
        if not modality:
            return True
        modes = [m.strip() for m in (getattr(s, "ADAPTER_CHAT_MODES", "") or "").split(",") if m.strip()]
        mode = (getattr(s, "ADAPTER_CHAT_MODE", "") or "").strip()
        if not await self._pick(page, "modality", modes=modes + ([mode] if mode else [])):
            logger.warning("Modality %s: button not found", modality)
            return False
        await page.click('[data-aidd-pick="toggle"]', timeout=5_000)
        await asyncio.sleep(0.5)
        if await self._choose(page, modality):
            return True
        logger.warning("Modality %s: option not found", modality)
        await page.keyboard.press("Escape")
        return False

    async def _model_is(self, page, model: str, seconds: float = 0) -> bool:
        """The model button shows exactly `model` (only it: not «model +1»)."""
        deadline = _MONOTONIC() + seconds
        while True:
            if await self._pick(page, "model", model=model, current=model) == "same":
                return True
            if _MONOTONIC() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def _search_open(self, page, seconds: float = 2.0, model: str = "") -> bool:
        """The model menu is open: its search box or the model's option is visible."""
        deadline = _MONOTONIC() + seconds
        while True:
            if await self._first_visible(page, 'input[placeholder*="earch" i]') is not None:
                return True
            if model:
                try:
                    if await page.evaluate(_PICK_OPTION_JS, model):
                        return True
                except Exception:
                    pass
            if _MONOTONIC() >= deadline:
                return False
            await asyncio.sleep(0.3)

    async def _open_model_menu(self, page, model: str) -> bool:
        """Open the model picker: the known chip, else the element whose text is the
        model name, else probe the toolbar (the clickable that opens a model search box)."""
        if await self._pick(page, "model", model=model):
            await page.click('[data-aidd-pick="toggle"]', timeout=5_000)
            if await self._search_open(page, 2.0, model):
                return True
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            await page.evaluate("() => document.querySelectorAll('[data-aidd-model-btn]')"
                                ".forEach((e) => e.removeAttribute('data-aidd-model-btn'))")
        s = self.settings
        modes = [m.strip() for m in (getattr(s, "ADAPTER_CHAT_MODES", "") or "").split(",") if m.strip()]
        mode = (getattr(s, "ADAPTER_CHAT_MODE", "") or "").strip()
        try:
            count = await page.evaluate(_TOOLBAR_CANDIDATES_JS, {
                "modes": modes + ([mode] if mode else []), "input": self._selectors["input"],
                "send": self._selectors["send"]})
        except Exception:
            count = 0
        for i in range(int(count or 0)):
            try:
                await page.click(f'[data-aidd-cand="{i}"]', timeout=3_000)
            except Exception:
                continue
            if await self._search_open(page, 1.5, model):
                await page.evaluate("(i) => document.querySelector(`[data-aidd-cand=\"${i}\"]`)"
                                    ".setAttribute('data-aidd-model-btn', '')", str(i))
                logger.info("Model picker: chip found by probing the toolbar (candidate %d of %d)", i + 1, count)
                return True
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
        return False

    async def _auto_pick_model(self, page, model: str) -> bool:
        """Open the model picker (the button showing a model name) and make `model`
        the only selected one: the list may be multi-select (ADAPTER_MODEL_CLEAR_LABEL,
        e.g. «Deselect all», is clicked first). Logs only found/not found."""
        if not getattr(self.settings, "ADAPTER_MODEL_AUTO", True):
            return False
        clear_label = (getattr(self.settings, "ADAPTER_MODEL_CLEAR_LABEL", "") or "").strip()
        search_box = 'input[placeholder*="earch" i]'
        steps = {"menu": False, "cleared": False, "modality": None, "option": False, "shown": False}
        try:
            steps["menu"] = await self._open_model_menu(page, model)
            if steps["menu"]:
                if clear_label:
                    steps["cleared"] = await self._choose(page, clear_label)
                    if steps["cleared"]:
                        # clearing also resets the modality (e.g. Code): set it again, then reopen
                        if await self._first_visible(page, search_box) is not None:
                            await page.keyboard.press("Escape")
                            await asyncio.sleep(0.5)
                        steps["modality"] = await self._set_modality(page)
                        await self._open_model_menu(page, model)
                steps["option"] = await self._choose(page, model, search=True)
                if await self._first_visible(page, 'input[placeholder*="earch" i]') is not None:
                    await page.keyboard.press("Escape")  # a multi-select list stays open
                    await asyncio.sleep(0.5)
            steps["shown"] = await self._model_is(page, model, 3)
        except Exception as exc:
            logger.warning("Model picker error: %s", type(exc).__name__)
        logger.info("Model picker for %s: %s", model, " ".join(f"{k}={v}" for k, v in steps.items()))
        if not steps["shown"]:
            try:
                logger.info("Model picker structure (no text): %s", await page.evaluate(_MODEL_DIAG_JS))
            except Exception:
                pass
        return steps["shown"]

    async def _nav(self, page, kind: str) -> bool:
        """Click a navigation control found by _NAV_JS (or by an ADAPTER_*_SELECTOR)."""
        setting = {"sidebar": "ADAPTER_SIDEBAR_SELECTOR", "history": "ADAPTER_HISTORY_LINK_SELECTOR",
                   "new": "ADAPTER_NEW_CHAT_SELECTOR"}[kind]
        selector = (getattr(self.settings, setting, "") or "").strip()
        try:
            if not await page.evaluate(_NAV_JS, {"kind": kind, "selector": selector}):
                return False
            await page.click("[data-aidd-nav]", timeout=5_000)
            await asyncio.sleep(2.0)
            return True
        except Exception:
            return False

    async def _new_chat_in_app(self, page) -> bool:
        """Some services fill the model list only after in-app navigation: open the
        most recent chat (opening the sidebar if needed), then a new chat - without
        reloading the page."""
        if not await self._nav(page, "history"):
            if await self._nav(page, "sidebar"):
                await self._nav(page, "history")
        opened = await self._nav(page, "new")
        if not opened and await self._nav(page, "sidebar"):  # the sidebar may have closed again
            opened = await self._nav(page, "new")
        logger.info("Model list workaround: new chat opened in the app: %s", opened)
        if opened:
            await self._wait_ready(page)
        return opened

    async def _ensure_model(self, page) -> None:
        """If the account has a model set: make sure the chat uses it (the
        model button shows its name). ADAPTER_MODEL_CLICKS ({model} = the name)
        can pick it; otherwise the user picks it once in the account's browser."""
        model = (self.expected_model or "").strip()
        if not model or not getattr(self.settings, "ADAPTER_MODEL_CHECK", True):
            return
        sel = self._model_selector(model)
        if await self._model_is(page, model, 5):
            return
        if await self._auto_pick_model(page, model):
            logger.info("Model selected: %s", model)
            return
        if getattr(self.settings, "ADAPTER_MODEL_AUTO", True) and await self._new_chat_in_app(page):
            try:
                await self._setup_chat(page)
            except Exception:
                logger.warning("Chat setup failed", exc_info=True)
            if await self._model_is(page, model, 3) or await self._auto_pick_model(page, model):
                logger.info("Model selected after the in-app new chat: %s", model)
                return
        clicks = (getattr(self.settings, "ADAPTER_MODEL_CLICKS", "") or "").replace("{model}", model.replace('"', ""))
        for selector in [part.strip() for part in clicks.split("||") if part.strip()]:
            try:
                await page.click(selector, timeout=5_000)
                await asyncio.sleep(0.5)
            except Exception:
                logger.warning("Model click not possible: %s", selector)
        if clicks and (await self._model_is(page, model, 5) or await self._wait_visible(page, sel, 1) is not None):
            logger.info("Model selected: %s", model)
            return
        raise RuntimeError(
            f"Model «{model}» is not selected in this account's chat and could not be picked automatically: "
            "check the model name (as the service shows it) or pick it once in the account's browser")

    async def send_message(self, page, text: str) -> None:
        """Type `text` into the chat input and send it."""
        if await self._is_login_required(page):
            await self._handle_login(page)
            await self._reopen(page)  # logging in may have navigated away from a new chat
        input_sel = self._selectors["input"]
        send_sel = self._selectors["send"]

        # Snapshot BEFORE sending: a fast reply must not end up in "before".
        count = await self._selector_count(page)
        # no selector match = the fallback reader: everything on the page now gets marked
        # «seen» and can never be the reply, so the baseline is simply empty
        self._before[page] = (count, await self._last_message_text(page)) if count else (0, "")
        await self._mark_seen(page)  # what is on the page now can never be the new reply
        self._last_sent[page] = text
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

    async def _handle_login(self, page) -> None:
        """Logged out: log in by ourselves if the account has credentials,
        otherwise (or if that fails) pause until a human logs in."""
        if await self.auto_login(page):
            return
        await self._pause(page, "login", self._is_login_required)

    async def auto_login(self, page) -> bool:
        """Do what a human does: «Log in» -> e-mail -> password. Never logs the
        e-mail or the password. Returns True when the page is logged in."""
        creds = self.login_credentials
        if not creds:
            return False
        email, password = creds
        per_hour = int(getattr(self.settings, "ADAPTER_AUTO_LOGIN_PER_HOUR", 2) or 0)
        key = hashlib.sha256(email.lower().encode()).hexdigest()
        now = _MONOTONIC()
        recent = [t for t in _AUTO_LOGINS.get(key, []) if now - t < 3600]
        if len(recent) >= per_hour:
            logger.warning("Auto-login: limit of %d attempts per hour reached; asking a human", per_hour)
            return False
        _AUTO_LOGINS[key] = recent + [now]
        s = self.settings
        email_sel, pw_sel = s.ADAPTER_LOGIN_EMAIL_SELECTOR, s.ADAPTER_LOGIN_PASSWORD_SELECTOR
        step = "open the login form"
        try:
            field = await self._first_visible(page, email_sel)
            if field is None and await self._first_visible(page, pw_sel) is None:
                if not await self._click_first(page, s.ADAPTER_LOGIN_OPEN_SELECTOR):
                    raise LookupError
                field = await self._wait_visible(page, email_sel, 15)
            if field is not None:
                step = "e-mail"
                await field.fill(email)
                await field.press("Enter")
                if await self._wait_visible(page, pw_sel, 6) is None:
                    await self._click_first(page, s.ADAPTER_LOGIN_SUBMIT_SELECTOR)
            step = "password"
            field = await self._wait_visible(page, pw_sel, 20)
            if field is None:
                raise LookupError
            await field.fill(password)
            await field.press("Enter")
            await asyncio.sleep(2.0)
            if await self._first_visible(page, pw_sel) is not None:
                await self._click_first(page, s.ADAPTER_LOGIN_SUBMIT_SELECTOR)
            step = "wait for the chat"
            for second in range(30):
                await asyncio.sleep(1.0)
                still_pw = await self._first_visible(page, pw_sel) is not None
                if still_pw and second >= 8:
                    break  # the password form stays: wrong password or an extra check
                if not await self._is_login_required(page) and not still_pw:
                    logger.info("Auto-login: logged in")
                    if self.on_logged_in is not None:
                        try:
                            await self.on_logged_in()
                        except Exception:
                            logger.warning("Auto-login: could not save the session", exc_info=True)
                    return True
                if await self._captcha_visible(page):
                    break  # a human has to solve it: the login pause takes over
            raise LookupError
        except Exception as exc:
            # Only the step name and error type: messages may contain page details.
            logger.warning("Auto-login failed at step «%s» (%s); asking a human", step, type(exc).__name__)
            return False

    async def _click_first(self, page, selectors: str) -> bool:
        """Click the first visible element of "||"-separated selectors (in that order)."""
        for selector in [part.strip() for part in (selectors or "").split("||") if part.strip()]:
            el = await self._first_visible(page, selector)
            if el is not None and await self._is_social_button(el):
                continue  # never «Continue with Google/Apple/...»: the account logs in by e-mail
            if el is not None:
                try:
                    await el.click(timeout=10_000)
                except TypeError:
                    await el.click()
                return True
        return False

    @staticmethod
    async def _is_social_button(el) -> bool:
        try:
            text = (await el.inner_text() or "").lower()
        except Exception:
            return False
        return any(w in text for w in ("google", "apple", "microsoft", "github", "facebook"))

    async def _wait_visible(self, page, selector: str, seconds: float):
        deadline = _MONOTONIC() + seconds
        while True:
            el = await self._first_visible(page, selector)
            if el is not None or _MONOTONIC() >= deadline:
                return el
            await asyncio.sleep(0.5)

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

    async def _refresh_chat(self, page) -> bool:
        """Reload the current chat in place. Never on the bare start page (the chat
        would be lost); our own reload is re-marked, so nothing is sent again."""
        try:
            path = urlsplit(page.url).path
        except Exception:
            path = ""
        if path in ("", "/"):
            logger.info("Chat looks stuck, but it has no own address yet: not refreshing")
            return False
        logger.info("Chat looks stuck (no new text for a while): refreshing the page")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=self._timeout_ms(
                self.settings.ADAPTER_NAVIGATE_TIMEOUT_SECONDS))
        except Exception:
            logger.warning("Chat refresh failed", exc_info=True)
            return False
        await self._wait_ready(page)
        await self._mark(page)  # our own reload: not a reason to send the message again
        return True

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
            self._marks[page] = token
        except Exception:
            self._marks.pop(page, None)

    async def _reloaded(self, page) -> bool:
        token = self._marks.get(page)
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
        await self._focus_input(page, el)
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

    async def _focus_input(self, page, el) -> None:
        """Click the input. Something may cover it (a menu, a rating panel, a
        pop-up, a captcha): press Escape and retry; a captcha or a pop-up that
        stays -> pause for the human; as a last resort focus it by script."""
        for attempt in range(3):
            try:
                await el.click(timeout=5_000)
                return
            except TypeError:  # minimal element objects (tests)
                await el.click()
                return
            except Exception:
                logger.info("The chat input is covered (attempt %d)", attempt + 1)
            if attempt == 0:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            elif attempt == 1:
                if await self._captcha_visible(page):
                    await self._pause(page, "captcha", lambda p: self._captcha_visible(p, True))
                elif await self._dialog_visible(page):
                    await self._pause(page, "dialog", self._dialog_visible)
        try:
            await el.evaluate("(e) => { e.scrollIntoView({block: 'center'}); e.focus(); }")
        except Exception as exc:
            raise RuntimeError(f"Could not focus chat input ({self._selectors['input']}): {exc}") from exc

    async def _dialog_visible(self, page) -> bool:
        try:
            return bool(await page.evaluate(_DIALOG_JS))
        except Exception:
            return False

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

        `timeout` is a limit on INACTIVITY: while the service is busy or the
        reply keeps growing, the wait goes on (agents may work for a long
        time), up to ADAPTER_MAX_REPLY_SECONDS in total. Time paused for a
        human does not count. If `reply_complete` is set and says a settled
        reply is not finished yet (an agent between steps), the wait goes on
        until it stays unchanged for ADAPTER_INCOMPLETE_SETTLE_SECONDS.
        Raises TimeoutError if no response arrives.
        """
        start = _MONOTONIC()
        hard_deadline = start + max(float(timeout), float(getattr(self.settings, "ADAPTER_MAX_REPLY_SECONDS", 3600)))
        last_progress = start
        poll = self.settings.ADAPTER_POLL_INTERVAL_SECONDS
        settle = float(getattr(self.settings, "ADAPTER_SETTLE_SECONDS", 3.0))
        incomplete_settle = float(getattr(self.settings, "ADAPTER_INCOMPLETE_SETTLE_SECONDS", 120.0))
        complete = getattr(self, "reply_complete", None)
        sent = self._last_sent.get(page, "")

        # Snapshot of the conversation before we wait, so we can detect a
        # genuinely new assistant message (not an echo of what we sent).
        snapshot = self._before.pop(page, None)
        if snapshot is None:  # wait_response without send_message (tests / reuse)
            snapshot = (await self._message_count(page), await self._last_message_text(page))
        before_count, before_text = snapshot
        candidate, candidate_since = "", 0.0
        seen = snapshot
        stuck_after = float(getattr(self.settings, "ADAPTER_STUCK_REFRESH_SECONDS", 300) or 0)
        last_change = start  # last time the reply TEXT changed (a busy indicator alone does not count)
        last_report = start
        resends = 0

        def paused(seconds: float) -> None:
            nonlocal hard_deadline, last_progress
            hard_deadline += seconds
            last_progress = _MONOTONIC()

        while True:
            state = await self.get_state(page)

            if state == "error":
                raise RuntimeError("Service entered an error state")
            if state == "login_required":
                await self._handle_login(page)
                # The reply (if any) was lost with the session: let the caller resend.
                raise RuntimeError("Session restored during the reply; the message must be sent again")
            if await self._captcha_visible(page):
                paused(await self._pause(page, "captcha", lambda p: self._captcha_visible(p, True)))
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
                    paused(0.0)
                    continue

            now = _MONOTONIC()
            after_count = await self._message_count(page)
            after_text = await self._last_message_text(page)
            if now - last_report >= 60:  # numbers only, never the text
                last_report = now
                logger.info(
                    "Waiting for the reply %.0fs: state=%s messages=%d (before %d) last_len=%d new=%s echo=%s "
                    "unchanged=%.0fs", now - start, state, after_count, before_count, len(after_text or ""),
                    bool(after_text) and after_text != before_text, bool(after_text) and _is_echo(after_text, sent),
                    now - last_change)
            if (after_count, after_text) != seen:
                last_change = now
            if state == "generating" or (after_count, after_text) != seen:
                last_progress, seen = now, (after_count, after_text)  # the service is working
            if stuck_after and now - last_change >= stuck_after:
                # «thinking» forever with nothing new on screen: the answer is often already
                # there - reload THIS chat (same tab, same URL) and read it again
                last_change = now
                if await self._refresh_chat(page):
                    candidate = ""
                    continue

            fresh = bool(
                state != "generating"
                and after_count > before_count
                and after_text
                and after_text != before_text
                and not _is_echo(after_text, sent)
            )
            if state == "generating":
                candidate = ""  # still working (agents work in several steps)
            elif fresh:
                if after_text != candidate:
                    candidate, candidate_since = after_text, now
                wait_for = settle
                if complete is not None and not complete(candidate):
                    wait_for = max(settle, incomplete_settle)  # an agent between steps: give it time
                if now - candidate_since >= wait_for:
                    if wait_for > settle:
                        logger.info("Taking an unfinished-looking reply after %.0fs without changes", wait_for)
                    return candidate

            if now - last_progress >= timeout or now >= hard_deadline:
                if candidate:
                    return candidate
                if now >= hard_deadline:
                    raise TimeoutError(f"No complete response from service within {hard_deadline - start:.0f}s")
                raise TimeoutError(f"No response from service within {timeout}s (no activity)")

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
