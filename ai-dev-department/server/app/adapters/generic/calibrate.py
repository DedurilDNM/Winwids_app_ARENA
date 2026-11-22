"""Account self-calibration: learn how THIS service page shows replies.

The server sends a test message whose answer markers are COMPUTED by the model
(«QX followed by the sum of 1234 and 2345» -> QX3579), so a marker found on the
page can only be in the reply, never in our own message. From where the markers
appear it derives, inside the page:

- a selector for reply messages (stable attributes: data-*, role, id prefix,
  class) that matches the reply and never our own message, verified by reading
  the reply back with the normal reader (code block fenced, markers present);
  if nothing stable exists: the «anchor» reader (blocks after our message);
- the busy indicator: elements that were visible while the answer was produced
  and are gone afterwards (none found = wait until the text stops changing);
- the chat input.

Privacy: only our own markers are compared. Nothing from the page text leaves
the page; the result holds structural selectors only, and logs hold booleans.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from datetime import datetime, timezone

from app.adapters.generic.adapter import _MESSAGE_TEXT_JS, _REPLY_FALLBACK_JS

logger = logging.getLogger(__name__)

PROFILE_VERSION = 1

CANARY_PROMPT = """{ask}
This is an automatic connection test. Do not use tools, do not build or run anything.
Reply with exactly these three parts and nothing else:
1. One line: the letters QX followed by the sum of {a} and {b}, without spaces (for example QX12).
2. A code block (three backticks) that contains one line: the letters ZW followed by the sum of {c} and {d}, without spaces.
3. The line: STATUS: DONE"""

_NORM_JS = r"""(t) => (t || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9а-яё]/g, '')"""

# Signatures (CSS selectors) of visible elements that can be a busy indicator.
_SIGNATURES_JS = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const q = (v) => JSON.stringify(v);
  const out = new Set();
  const sel = 'button, [role="button"], [aria-busy], [role="progressbar"], [role="status"], [data-state], '
    + '[data-testid], [aria-label], [class*="spin" i], [class*="load" i], [class*="typing" i], '
    + '[class*="generat" i], [class*="stream" i], [class*="thinking" i], [class*="pending" i]';
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e) || e.closest('textarea, [contenteditable="true"]')) continue;
    const tag = e.tagName.toLowerCase();
    const al = e.getAttribute('aria-label');
    if (al && al.length < 60) out.add(`${tag}[aria-label=${q(al)}]`);
    const tid = e.getAttribute('data-testid');
    if (tid && tid.length < 60) out.add(`[data-testid=${q(tid)}]`);
    if (e.getAttribute('aria-busy') === 'true') out.add('[aria-busy="true"]');
    const role = e.getAttribute('role');
    if (role === 'progressbar' || role === 'status') out.add(`[role=${q(role)}]`);
    const ds = e.getAttribute('data-state');
    if (ds && /stream|generat|load|busy|pending|running|think/i.test(ds)) out.add(`${tag}[data-state=${q(ds)}]`);
    for (const c of e.classList) {
      if (c.length < 60 && /spin|load|typing|generat|stream|pending|thinking/i.test(c)) out.add(`${tag}.${CSS.escape(c)}`);
    }
  }
  return [...out].slice(0, 400);
}
"""

# Does the page show both reply markers yet? + a cheap «page text size» for stability.
_PRESENCE_JS = r"""
(args) => {
  const norm = __NORM__;
  const t = norm(document.body ? document.body.innerText : '');
  return { found: t.includes(norm(args.one)) && t.includes(norm(args.two)), size: t.length };
}
""".replace("__NORM__", _NORM_JS)

# Locate the reply and our message, derive selector candidates and verify them.
_LOCATE_JS = r"""
(args) => {
  const norm = __NORM__;
  const msgText = __MESSAGE_TEXT__;
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const one = norm(args.one), two = norm(args.two), ask = norm(args.ask);
  const all = [...document.querySelectorAll('body *')]
    .filter((e) => !e.closest('textarea, input, [contenteditable="true"], script, style'));
  const deepest = (test) => {
    const hits = all.filter((e) => vis(e) && test(norm(e.textContent)));
    return hits.filter((e) => ![...e.children].some((c) => test(norm(c.textContent))));
  };
  const replies = deepest((t) => t.includes(one) && t.includes(two));
  if (!replies.length) return { reply: false };
  const reply = replies[replies.length - 1];
  const users = deepest((t) => t.includes(ask));
  const user = users.length ? users[users.length - 1] : null;
  const up = (e) => { const a = []; for (let x = e; x; x = x.parentElement) a.push(x); return a; };
  const rpath = up(reply);
  let rootIdx = Math.min(rpath.length - 2, 6);
  let userZone = null;
  if (user) {
    const upath = up(user);
    const uset = new Set(upath);
    const lca = rpath.find((x) => uset.has(x));
    rootIdx = Math.max(0, rpath.indexOf(lca) - 1);
    userZone = upath[Math.max(0, upath.indexOf(lca) - 1)];
  }
  const nodes = rpath.slice(0, rootIdx + 1).reverse();  // reply root ... reply content
  const q = (v) => JSON.stringify(v);
  const cands = new Map();
  const add = (sel, kind, depth) => { if (!cands.has(sel)) cands.set(sel, { sel, kind, depth }); };
  nodes.forEach((n, depth) => {
    const tag = n.tagName.toLowerCase();
    for (const at of n.attributes) {
      const name = at.name, v = at.value;
      if (name.startsWith('data-aidd')) continue;
      if (name.startsWith('data-')) {
        if (v && v.length <= 40 && !/[0-9a-f]{8}-|\d{4,}/i.test(v)) add(`${tag}[${name}=${q(v)}]`, 'data', depth);
        add(`${tag}[${name}]`, 'data', depth);
      } else if (name === 'role' || name === 'aria-roledescription') {
        add(`${tag}[${name}=${q(v)}]`, 'role', depth);
      } else if (name === 'id') {
        const m = v.match(/^(.*?[-_:])[0-9a-z-]*\d[0-9a-z-]*$/i);
        if (m && m[1].length >= 3) add(`${tag}[id^=${q(m[1])}]`, 'id', depth);
      }
    }
    for (const c of n.classList) {
      if (c.length <= 40 && /[a-z]{3}/i.test(c)) add(`${tag}.${CSS.escape(c)}`, 'class', depth);
    }
  });
  const fencedTwo = (text) => text.split('```').some((part, i) => i % 2 === 1 && norm(part).includes(two));
  const good = [];
  for (const c of cands.values()) {
    let m;
    try { m = [...document.querySelectorAll(c.sel)].filter(vis); } catch (e) { continue; }
    if (!m.length || m[m.length - 1] !== nodes[c.depth]) continue;  // the latest match must be the reply
    if (userZone && m.some((x) => userZone.contains(x) || x.contains(userZone))) continue;  // never our message
    const text = msgText(m[m.length - 1]);
    const nt = norm(text);
    if (!nt.includes(one) || !nt.includes(two)) continue;
    good.push({ sel: c.sel, kind: c.kind, depth: c.depth, matches: m.length, fenced: fencedTwo(text) });
  }
  const rank = { data: 0, role: 0, id: 1, class: 2 };
  good.sort((a, b) => (b.fenced - a.fenced) || (rank[a.kind] - rank[b.kind]) || (a.depth - b.depth) || (a.matches - b.matches));
  return { reply: true, user: !!user, best: good.slice(0, 3).map((g) => ({ sel: g.sel, fenced: g.fenced, kind: g.kind })) };
}
""".replace("__NORM__", _NORM_JS).replace("__MESSAGE_TEXT__", _MESSAGE_TEXT_JS.strip())

_INPUT_GUESS = 'textarea, div[contenteditable="true"], [contenteditable="true"][role="textbox"]'


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def profile_key(settings) -> str:
    """Profiles are per chat modality (Text / Code ...): pages differ between them."""
    return (getattr(settings, "ADAPTER_MODALITY", "") or "").strip() or "default"


def _norm(text: str) -> str:
    import re
    import unicodedata

    return re.sub(r"[^a-z0-9а-яё]", "", unicodedata.normalize("NFKD", text or "").lower())


async def calibrate(adapter, context, *, timeout: float = 300, progress=None) -> dict:
    """Run the calibration in a new chat of `context`. Returns a profile dict
    ({"ok": bool, ...}); never raises for a page problem (ok=False + reason)."""
    async def step(text: str) -> None:
        if progress is not None:
            try:
                await progress(text)
            except Exception:
                pass

    started = time.monotonic()
    profile: dict = {"version": PROFILE_VERSION, "modality": profile_key(adapter.settings), "ok": False,
                     "checked_at": _now_iso()}
    page = None
    try:
        await step("opening a new chat")
        page = await adapter.open_chat(context, "calibration")

        # the chat input: the configured one, else a generic guess
        if await adapter._first_visible(page, adapter._selectors["input"]) is not None:
            profile["input"] = ""
        elif await adapter._first_visible(page, _INPUT_GUESS) is not None:
            profile["input"] = _INPUT_GUESS
        else:
            profile["reason"] = "no chat input on the page"
            return profile

        rnd = random.SystemRandom()
        a, b, c, d = (rnd.randint(1000, 4999) for _ in range(4))
        ask = f"AIDD-CHECK-{secrets.token_hex(3).upper()}"
        one, two = f"QX{a + b}", f"ZW{c + d}"
        prompt = CANARY_PROMPT.format(ask=ask, a=a, b=b, c=c, d=d)

        before = set(await page.evaluate(_SIGNATURES_JS))
        await step("sending the test message")
        saved_input = adapter.profile
        adapter.profile = {"input": profile["input"]} if profile["input"] else None
        try:
            await adapter.send_message(page, prompt)
        finally:
            adapter.profile = saved_input
        sent_at = time.monotonic()

        await step("waiting for the answer")
        timeline: dict[str, list[float]] = {}
        reply_at = None
        last_size, stable_since = -1, None
        sigs: list[str] = []
        last_sigs, sigs_since = None, time.monotonic()
        while time.monotonic() - sent_at < timeout:
            if await adapter._captcha_visible(page):
                await adapter._pause(page, "captcha", lambda p: adapter._captcha_visible(p, True))
            now = time.monotonic()
            sigs = await page.evaluate(_SIGNATURES_JS)
            for sig in sigs:
                seen = timeline.setdefault(sig, [now, now])
                seen[1] = now
            if set(sigs) != last_sigs:
                last_sigs, sigs_since = set(sigs), now
            presence = await page.evaluate(_PRESENCE_JS, {"one": one, "two": two})
            if presence["found"]:
                reply_at = reply_at or now
                if presence["size"] != last_size:
                    last_size, stable_since = presence["size"], now
                elif now - stable_since >= 5 and now - sigs_since >= 5:
                    break  # the text and the page controls stopped changing: the answer is complete
            await asyncio.sleep(0.5)
        if reply_at is None:
            profile["reason"] = "no answer with the test markers"
            return profile
        profile["reply_seconds"] = round(reply_at - sent_at, 1)

        await step("finding the answer on the page")
        final = set(await page.evaluate(_SIGNATURES_JS))
        busy = _pick_busy(timeline, before, final, sent_at, reply_at)
        profile["busy"] = busy or ""
        located = await page.evaluate(_LOCATE_JS, {"one": one, "two": two, "ask": ask})
        best = (located or {}).get("best") or []
        if best:
            profile["reader"], profile["message"] = "selector", best[0]["sel"]
        else:
            profile["reader"], profile["message"] = "anchor", ""

        # verify with the NORMAL reader, exactly as tasks will read replies
        await step("checking that the answer is read correctly")
        adapter.profile = profile
        text = await adapter._last_message_text(page)
        ok_markers = _norm(one) in _norm(text) and _norm(two) in _norm(text)
        if not ok_markers and profile["reader"] == "selector":  # one more chance: the anchor reader
            profile["reader"], profile["message"] = "anchor", ""
            text = await adapter._last_message_text(page)
            ok_markers = _norm(one) in _norm(text) and _norm(two) in _norm(text)
        fenced = any(i % 2 == 1 and _norm(two) in _norm(part) for i, part in enumerate(text.split("```")))
        profile["checks"] = {"markers": ok_markers, "code_block": fenced, "status_line": "status:done" in _norm(text).replace(" ", "")
                             or "statusdone" in _norm(text), "own_message_found": bool(located.get("user"))}
        profile["ok"] = ok_markers
        if not ok_markers:
            profile["reason"] = "the answer is on the page but could not be read back"
        return profile
    except Exception as exc:  # a page problem: report, never crash the caller
        logger.warning("Calibration failed: %s", type(exc).__name__, exc_info=True)
        profile["reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return profile
    finally:
        profile["seconds"] = round(time.monotonic() - started, 1)
        logger.info("Calibration: ok=%s reader=%s busy=%s markers=%s seconds=%s", profile.get("ok"),
                    profile.get("reader"), bool(profile.get("busy")), (profile.get("checks") or {}).get("markers"),
                    profile.get("seconds"))
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


def _pick_busy(timeline: dict, before: set, final: set, sent_at: float, reply_at: float) -> str | None:
    """A busy indicator: appeared after sending, visible while the answer came, gone at the end."""
    best, best_span = None, 0.0
    for sig, (first, last) in timeline.items():
        if sig in before or sig in final:
            continue
        if first > reply_at + 2 or last < reply_at - 1:
            continue
        span = last - first
        score = span + (5 if "stop" in sig.lower() else 0) + (3 if "aria-busy" in sig else 0)
        if score > best_span:
            best, best_span = sig, score
    return best


async def apply_profile(session_factory, account_id: int, profile: dict) -> None:
    """Store a successful profile on the account (per modality)."""
    from app.models import Account

    async with session_factory() as session:
        account = await session.get(Account, account_id)
        if account is None:
            return
        profiles = dict(account.adapter_profile or {})
        profiles[profile.get("modality") or "default"] = profile
        account.adapter_profile = profiles
        await session.commit()
