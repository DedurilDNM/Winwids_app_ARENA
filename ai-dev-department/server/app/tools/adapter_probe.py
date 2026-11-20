"""Adapter probe: inspect the external service's chat page and print a short
report used to configure the ADAPTER_* selectors in .env.

    cd server && .venv/bin/python -m app.tools.adapter_probe [--account ID] [--url URL] [--no-send]

What it does (in a separate headless browser with the account's saved
session - the account's live browser in the API process is not touched):
1. opens SERVICE_URL (or --url), reports whether a login form is visible;
2. lists visible input fields and buttons with their attributes;
3. sends "Reply with only the word PONG<digits>", watches the page for up to
   90 s and reports which elements hold the user message and the reply, and
   what changes while the answer is generated (stop button, aria-busy...);
4. runs the CURRENT adapter configuration end-to-end and shows its result.
Screenshots go to data/probe/. Nothing secret is printed (no cookies/tokens),
and no page URL, title or page text either - only element structure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time

from app.config import DATA_DIR, get_settings

DESCRIBE_JS = """
(el) => {
  const attrs = ["id", "name", "type", "role", "placeholder", "aria-label", "title", "data-testid", "data-test-id", "data-role", "data-message-author-role"];
  const parts = [el.tagName.toLowerCase()];
  for (const a of attrs) { const v = el.getAttribute(a); if (v) parts.push(`[${a}=${JSON.stringify(v.slice(0, 40))}]`); }
  const cls = (el.getAttribute("class") || "").split(/\\s+/).filter(Boolean).slice(0, 4);
  if (cls.length) parts.push("." + cls.join("."));
  if (el.isContentEditable && el.getAttribute("contenteditable") !== null) parts.push("[contenteditable]");
  return parts.join("");
}
"""

FIND_JS = """
([needle, exact, describe]) => {
  const d = eval(describe);
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.closest("textarea, [contenteditable=true], input")) continue;
    const t = (n.innerText || "").trim();
    if (!(exact ? t === needle : t.includes(needle))) continue;
    // deepest only: skip if a child also matches
    const child = [...n.children].some(c => { const ct = (c.innerText || "").trim(); return exact ? ct === needle : ct.includes(needle); });
    if (child) continue;
    const chain = [];
    let p = n;
    for (let i = 0; i < 8 && p && p !== document.body; i++, p = p.parentElement) chain.push(d(p));
    out.push(chain);
  }
  return out.slice(0, 4);
}
"""

STATE_JS = """
([describe]) => {
  const d = eval(describe);
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const busy = [...document.querySelectorAll("[aria-busy=true], [data-state*=generat i], [class*=generat i], [class*=streaming i], [class*=loading i]")].filter(vis).map(d);
  const stop = [...document.querySelectorAll("button, [role=button]")].filter(vis)
    .filter(b => /stop|cancel/i.test((b.getAttribute("aria-label") || "") + " " + (b.getAttribute("title") || "") + " " + (b.getAttribute("data-testid") || "") + " " + (b.innerText || "")))
    .map(d);
  return { busy: [...new Set(busy)].slice(0, 5), stop: [...new Set(stop)].slice(0, 3) };
}
"""


OVERLAYS_JS = """
([describe]) => {
  const d = eval(describe);
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== "hidden"; };
  const dialogs = [...document.querySelectorAll("[role=dialog], [role=alertdialog], [aria-modal=true], dialog[open], [role=alert]")]
    .filter(vis).map((e) => d(e) + " :: " + ((e.innerText || "").trim().length) + " chars of text");
  const frames = [...document.querySelectorAll("iframe")].filter(vis)
    .map((f) => f.src || "")
    .filter((s) => /captcha|challenge|turnstile|recaptcha|hcaptcha|cloudflare/i.test(s))
    .map((s) => (s.match(/captcha|challenge|turnstile|recaptcha|hcaptcha|cloudflare/i) || ["?"])[0].toLowerCase());
  return { dialogs: [...new Set(dialogs)].slice(0, 5), captcha: frames.slice(0, 3) };
}
"""

INPUT_TEXT_JS = "(el) => (el.value !== undefined ? el.value : el.innerText) || ''"


def out(line: str = "") -> None:
    print(line, flush=True)


async def visible(page, selector: str, limit: int = 12) -> list[str]:
    result = []
    for el in await page.query_selector_all(selector):
        try:
            if await el.is_visible():
                result.append(await el.evaluate(DESCRIBE_JS))
        except Exception:
            continue
        if len(result) >= limit:
            break
    return result


async def pick_account(account_id: int | None):
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import Account

    await init_db()
    async with SessionLocal() as s:
        q = select(Account).where(Account.storage_state_encrypted.isnot(None))
        if account_id:
            q = q.where(Account.id == account_id)
        else:
            q = q.where(Account.status == "active").order_by(Account.id)
        return (await s.scalars(q)).first()


async def overlays(page, label: str) -> None:
    info = await page.evaluate(OVERLAYS_JS, [DESCRIBE_JS])
    out(f"{label}: dialogs={info['dialogs'] or 'none'} captcha_frames={info['captcha'] or 'none'}")


async def main(args) -> int:
    from app.adapters.generic.adapter import Adapter
    from app.security import decrypt_bytes
    from app.services.browser_manager import BrowserManager

    settings = get_settings()
    account = await pick_account(args.account)
    if account is None:
        out("!! No account with a saved session. Log into one in the app first («Готово, я вошёл»).")
        return 1
    url = args.url or settings.SERVICE_URL
    shots = DATA_DIR / "probe"
    shots.mkdir(parents=True, exist_ok=True)

    out("=== ADAPTER PROBE REPORT ===")
    display = f":{settings.VNC_SHARED_DISPLAY}"
    from app.services.vnc_manager import VncManager

    headed = not args.headless and VncManager.x_display_alive(settings.VNC_SHARED_DISPLAY)
    out(f"account: #{account.id} ({account.default_role}), proxy: {'yes' if settings.PROXY_URL else 'no'}, "
        f"mode: {'headed on ' + display + ' (like the real agent browsers)' if headed else 'headless'}")
    bm = BrowserManager()
    try:
        context = await bm.launch(account.id, storage_state=json.loads(decrypt_bytes(account.storage_state_encrypted)),
                                  headless=not headed, display=display if headed else None, open_url=False)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(6)
        await page.screenshot(path=str(shots / "1-opened.png"))
        out("page opened")  # URL, title and page text are intentionally never printed
        login = await visible(page, settings.ADAPTER_LOGIN_SELECTOR or "input[type=password]", 3)
        out(f"login form visible: {'YES -> session not logged in: ' + '; '.join(login) if login else 'no'}")
        await overlays(page, "on open")

        out("\n-- inputs (visible) --")
        inputs = await visible(page, "textarea, [contenteditable=true], [role=textbox], input[type=text], input:not([type])")
        for d in inputs:
            out(f"  {d}")
        out(f"  current ADAPTER_INPUT_SELECTOR matches: {await visible(page, settings.ADAPTER_INPUT_SELECTOR, 3)}")

        out("\n-- buttons (visible, first 20) --")
        for d in await visible(page, "button, [role=button], input[type=submit]", 20):
            out(f"  {d}")
        out(f"  current ADAPTER_SEND_SELECTOR matches: {await visible(page, settings.ADAPTER_SEND_SELECTOR, 3)}")

        if args.no_send:
            return 0

        marker = f"PONG{random.randint(1000, 9999)}"
        prompt = f"Reply with only the word {marker}"
        box = None
        for sel in (settings.ADAPTER_INPUT_SELECTOR, "textarea", "[contenteditable=true]", "[role=textbox]"):
            for el in await page.query_selector_all(sel):
                if await el.is_visible():
                    box = el
                    break
            if box:
                break
        if box is None:
            out("\n!! No visible input to type into. Send the screenshot data/probe/1-opened.png.")
            return 2
        out(f"\n-- test message: typing into {await box.evaluate(DESCRIBE_JS)} and pressing Enter --")
        url_before = page.url
        await box.click()
        await page.keyboard.type(prompt, delay=15)
        out(f"input text before Enter: {len(await box.evaluate(INPUT_TEXT_JS))} chars")
        await page.keyboard.press("Enter")
        await asyncio.sleep(3)
        left = len(await box.evaluate(INPUT_TEXT_JS)) if await box.is_visible() else 0
        out(f"after Enter: input has {left} chars left, url changed: {page.url != url_before}")
        await overlays(page, "after Enter")
        if left and page.url == url_before:
            send = None
            for el in await page.query_selector_all(settings.ADAPTER_SEND_SELECTOR):
                if await el.is_visible():
                    send = el
                    break
            if send is not None:
                out(f"Enter did not send -> clicking {await send.evaluate(DESCRIBE_JS)} (disabled={await send.is_disabled()})")
                try:
                    await send.click(timeout=10_000)
                except Exception as exc:
                    out(f"click failed: {type(exc).__name__}: {str(exc).splitlines()[0][:150]}")
                await asyncio.sleep(3)
                left = len(await box.evaluate(INPUT_TEXT_JS)) if await box.is_visible() else 0
                out(f"after click: input has {left} chars left, url changed: {page.url != url_before}")
                await overlays(page, "after click")
        t0 = time.monotonic()
        seen_busy, seen_stop, reply_at = set(), set(), None
        while time.monotonic() - t0 < 90:
            await asyncio.sleep(1)
            st = await page.evaluate(STATE_JS, [DESCRIBE_JS])
            seen_busy.update(st["busy"])
            seen_stop.update(st["stop"])
            found = await page.evaluate(FIND_JS, [marker, True, DESCRIBE_JS])
            if found and reply_at is None:
                reply_at = time.monotonic() - t0
            if reply_at is not None and not st["stop"] and not st["busy"] and time.monotonic() - t0 > reply_at + 3:
                break
        await page.screenshot(path=str(shots / "2-after-reply.png"))
        out(f"reply with {marker}: {'after %.0fs' % reply_at if reply_at else 'NOT FOUND within 90s'}  (url changed: {page.url != url_before})")
        if not reply_at:
            await overlays(page, "after waiting")
        out("while generating, stop buttons: " + ("; ".join(sorted(seen_stop)) or "none seen"))
        out("while generating, busy markers: " + ("; ".join(sorted(seen_busy)) or "none seen"))
        out("\n-- USER message element (innermost -> outer) --")
        for chain in (await page.evaluate(FIND_JS, ["Reply with only the word", False, DESCRIBE_JS]))[:1]:
            for i, d in enumerate(chain):
                out(f"  {'  ' * i}{d}")
        out("-- REPLY element (innermost -> outer) --")
        for chain in (await page.evaluate(FIND_JS, [marker, True, DESCRIBE_JS]))[:2]:
            for i, d in enumerate(chain):
                out(f"  {'  ' * i}{d}")
            out("  --")
        out(f"current ADAPTER_MESSAGE_SELECTOR count: {len(await page.query_selector_all(settings.ADAPTER_MESSAGE_SELECTOR))}")

        out("\n-- current adapter config, end to end --")
        adapter = Adapter()
        marker2 = f"PONG{random.randint(1000, 9999)}"
        try:
            await adapter.send_message(page, f"Reply with only the word {marker2}")
            text = await asyncio.wait_for(adapter.wait_response(page, timeout=90), timeout=120)
            ok = text.strip().strip(".").endswith(marker2)
            out(f"adapter reply: {text[:120]!r} -> {'OK' if ok else 'WRONG (expected ' + marker2 + ')'}")
        except Exception as exc:
            out(f"adapter failed: {type(exc).__name__}: {str(exc)[:200]}")

        out("\n-- output contract round trip (what agents must do) --")
        contract_prompt = (
            "Reply strictly in this format and nothing else:\n\n"
            "FILE: hello.py\n```python\nprint(\"hello\")\n```\n\n"
            "STATUS: DONE\nSUMMARY: probe"
        )
        try:
            from app.services.agent_output import AgentOutputError, parse_agent_output

            await adapter.send_message(page, contract_prompt)
            text = await asyncio.wait_for(adapter.wait_response(page, timeout=120), timeout=150)
            try:
                parsed = parse_agent_output(text)
                good = parsed.files.get("hello.py", "").strip() == 'print("hello")'
                out(f"parsed: status={parsed.status} files={list(parsed.files)} -> {'OK' if good else 'CONTENT MANGLED: ' + repr(parsed.files)[:200]}")
            except AgentOutputError as exc:
                out(f"NOT PARSED: {exc}")
            out("raw reply as the adapter sees it:")
            for line in text.splitlines()[:14]:
                out(f"  | {line[:100]}")
        except Exception as exc:
            out(f"contract check failed: {type(exc).__name__}: {str(exc)[:200]}")
        out("=== END (screenshots: server/data/probe/) ===")
        return 0
    finally:
        await bm.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", type=int, help="account id (default: first active)")
    parser.add_argument("--url", help="page to open (default: SERVICE_URL)")
    parser.add_argument("--no-send", action="store_true", help="only list inputs/buttons, do not send a message")
    parser.add_argument("--headless", action="store_true", help="force headless (default: headed on the shared Xvfb display)")
    sys.exit(asyncio.run(main(parser.parse_args())))
