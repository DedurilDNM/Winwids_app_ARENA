"""Reading a finished reply with its «copy» button (real browser, fake service page).

The page reader is broken on purpose (its selector never finds a new message), as on
the real site when the orchestrator's reply was on screen but never read. The copy
button under the finished answer must give the whole answer (markdown, code fences),
never our own message (its copy button), never a code block's copy button, never
a half-written answer, and never a stale clipboard."""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

CHROME = os.environ.get("E2E_CHROMIUM") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
pytestmark = pytest.mark.skipif(not (os.path.exists(CHROME) or shutil.which("chromium")), reason="no browser")

SERVICE = "https://service.example.com/"

REPLY_MD = 'Plan:\n\n```json\n[\n  {"title": "a", "n": NUM}\n]\n```'


def _page(how: str = "writeText", early_copy: bool = False, hover_only: bool = False) -> str:
    """how: writeText | exec (execCommand + copy event) | item (clipboard.write)."""
    return """<!doctype html><meta charset="utf-8">
<style>.bar.hover{display:none}.msg:hover .bar.hover{display:flex}</style>
<div class="msg-old">old</div>
<main id="list"></main>
<textarea id="box"></textarea><button id="send" aria-label="Send">Send</button>
<script>
const HOW = '""" + how + """', EARLY = """ + ("true" if early_copy else "false") + """, HOVER = """ + (
        "true" if hover_only else "false") + """;
let n = 0;
async function put(text) {
  if (HOW === 'writeText') { try { await navigator.clipboard.writeText(text); } catch (e) {} }
  else if (HOW === 'item') {
    try { await navigator.clipboard.write([new ClipboardItem({'text/plain': new Blob([text], {type: 'text/plain'})})]); } catch (e) {}
  } else {
    const t = document.createElement('textarea'); t.value = text; document.body.append(t); t.select();
    const h = (e) => { e.clipboardData.setData('text/plain', text); e.preventDefault(); };
    document.addEventListener('copy', h, {once: true}); document.execCommand('copy'); t.remove();
  }
}
function icon(name) { return '<svg class="lucide lucide-' + name + '" width="16" height="16"><path d="M0 0h16v16H0z"/></svg>'; }
document.getElementById('send').onclick = () => {
  const box = document.getElementById('box'); const text = box.value; box.value = ''; n += 1;
  const u = document.createElement('div'); u.className = 'msg';
  u.innerHTML = '<p></p><div class="bar"><button aria-label="Copy message">' + icon('copy') + '</button><button aria-label="Edit">' + icon('pencil') + '</button></div>';
  u.querySelector('p').textContent = text;
  u.querySelector('button').onclick = () => put(text);
  document.getElementById('list').append(u);
  const md = """ + repr(REPLY_MD) + """.replace('NUM', String(n));
  const r = document.createElement('div'); r.className = 'msg';
  r.innerHTML = '<div class="body"></div>';
  const body = r.querySelector('.body');
  let shown = '';
  const bar = () => {
    const b = document.createElement('div'); b.className = 'bar' + (HOVER ? ' hover' : '');
    b.innerHTML = ['thumbs-up', 'thumbs-down', 'refresh-cw', 'copy'].map((i) => '<button>' + icon(i) + '</button>').join('');
    b.lastChild.onclick = () => put(shown);
    r.append(b);
  };
  setTimeout(() => { document.getElementById('list').append(r); if (EARLY) bar(); }, 300);
  const steps = [md.slice(0, 7), md.slice(0, 20), md];
  steps.forEach((part, i) => setTimeout(() => {
    shown = part;
    body.innerHTML = '<p>Plan:</p><div class="code"><div class="hdr"><span>json</span><button>' + icon('copy') +
      '</button><button aria-label="Copy code">c</button></div><pre><code></code></pre></div>';
    body.querySelector('code').textContent = part.slice(14);
    body.querySelector('.hdr button').onclick = () => put('CODE ONLY');
    if (i === steps.length - 1 && !EARLY) bar();
  }, 600 + i * 1500));
};
</script>"""


def _settings(**kw):
    from app.config import Settings

    base = dict(SERVICE_URL=SERVICE, ADAPTER_INPUT_SELECTOR="#box", ADAPTER_SEND_SELECTOR="#send",
                ADAPTER_LOGIN_SELECTOR="#login", ADAPTER_MESSAGE_SELECTOR=".msg-old",
                ADAPTER_STATE_SELECTOR=".no-such-busy", ADAPTER_POLL_INTERVAL_SECONDS=0.2,
                ADAPTER_SETTLE_SECONDS=1.0, ADAPTER_AFTER_REPLY_ESCAPE=False, ADAPTER_REPLY_FALLBACK=False)
    base.update(kw)
    return Settings(**base)


def _run(html: str, prompts: list[str], timeout: float = 20, patch=None, **settings):
    from playwright.async_api import async_playwright

    from app.adapters.generic.adapter import Adapter

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context()

            async def serve(route):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

            await context.route("https://service.example.com/**", serve)
            adapter = Adapter(settings=_settings(**settings))
            if patch:
                patch(adapter)
            page = await adapter.open_chat(context, "task-1")
            replies = []
            for prompt in prompts:
                await adapter.send_message(page, prompt)
                replies.append(await adapter.wait_response(page, timeout=timeout))
            await browser.close()
            return replies

    return asyncio.run(go())


PROMPT = "You are the orchestrator. Make a plan for the project, as JSON in a code block."


@pytest.mark.parametrize("how", ["writeText", "exec", "item"])
def test_reply_is_read_by_its_copy_button_when_the_page_reader_finds_nothing(how):
    [reply] = _run(_page(how), [PROMPT])
    assert reply == REPLY_MD.replace("NUM", "1")  # whole answer, with the code fence


def test_a_copy_button_shown_while_the_answer_is_written_waits_for_the_whole_answer():
    [reply] = _run(_page(early_copy=True), [PROMPT])
    assert reply == REPLY_MD.replace("NUM", "1")


def test_the_copy_button_shown_only_on_hover():
    [reply] = _run(_page(hover_only=True), [PROMPT])
    assert reply == REPLY_MD.replace("NUM", "1")


def test_second_message_in_the_same_chat_gets_the_second_answer():
    replies = _run(_page(), [PROMPT, "Now review the work. Answer as JSON again."])
    assert replies == [REPLY_MD.replace("NUM", "1"), REPLY_MD.replace("NUM", "2")]


def test_unfinished_json_is_not_taken_while_the_answer_is_still_written():
    def patch(adapter):
        adapter.reply_complete = lambda text: text.rstrip().endswith("```")

    [reply] = _run(_page(early_copy=True), [PROMPT], patch=patch)
    assert reply.endswith("```") and '"n": 1' in reply


def test_clipboard_paste_fallback_when_page_hooks_see_nothing():
    """Hooks in the page world can fail (another script world): the clipboard itself is read
    by a real paste into our own field, after a known text was put there first."""
    from app.adapters.generic import adapter as mod

    def patch(adapter):
        async def no_hooks(page, script):
            if script == mod._CLIP_READ_JS:
                return [None, None]
            return [True, None]
        adapter._clip_eval = no_hooks

    async def check_paste_works():
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = await browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = await context.new_page()
            await page.set_content("<textarea id=a>xyz</textarea><textarea id=b></textarea>")
            await page.focus("#a")
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Control+c")
            await page.focus("#b")
            await page.keyboard.press("Control+v")
            ok = await page.input_value("#b") == "xyz"
            await browser.close()
            return ok

    if not asyncio.run(check_paste_works()):
        pytest.skip("this browser build has no working keyboard clipboard")
    [reply] = _run(_page("exec"), [PROMPT], patch=patch)
    assert reply == REPLY_MD.replace("NUM", "1")


def test_no_copy_button_falls_back_to_the_page_reader():
    """A service without copy buttons: the normal reader still works."""
    html = _page().replace("['thumbs-up', 'thumbs-down', 'refresh-cw', 'copy']", "['thumbs-up']")
    [reply] = _run(html, [PROMPT], ADAPTER_MESSAGE_SELECTOR=".msg .body", ADAPTER_SETTLE_SECONDS=2.0)
    assert '"n": 1' in reply and reply != REPLY_MD.replace("NUM", "1")  # read from the page, not copied


def test_copy_reader_can_be_switched_off():
    with pytest.raises(TimeoutError):
        _run(_page(), [PROMPT], timeout=6, ADAPTER_COPY_READER=False, ADAPTER_MAX_REPLY_SECONDS=6)


def _latest_only_page(copy_as_div: bool = False) -> str:
    """Like the real site: the message selector matches ONE element (the latest answer)
    whose text is replaced - the number of matches never grows."""
    copy_btn = ('<div class="act" style="cursor:pointer"><mat-icon class="mat-icon">content_copy</mat-icon></div>'
                if copy_as_div else "")
    return """<!doctype html><meta charset="utf-8">
<div id="chat"><div class="latest"><p>previous answer from an older chat</p></div></div>
<textarea id="box"></textarea><button id="send" aria-label="Send">Send</button>
<script>
document.getElementById('send').onclick = () => {
  const box = document.getElementById('box'); const text = box.value; box.value = '';
  const u = document.createElement('div'); u.className = 'user'; u.textContent = text;
  const old = document.querySelector('.latest'); old.classList.remove('latest'); old.classList.add('done');
  old.after(u);
  const r = document.createElement('div'); r.className = 'latest';
  setTimeout(() => { u.after(r); r.innerHTML = '<p>STATUS: DO</p>'; }, 400);
  setTimeout(() => { r.innerHTML = '<p>FILE: a.txt</p><p>STATUS: DONE</p>""" + copy_btn.replace("'", "\\'") + """';
    const b = r.querySelector('.act'); if (b) b.onclick = () => navigator.clipboard.writeText('FILE: a.txt\\n\\nSTATUS: DONE (copied)').catch(() => {}); }, 1500);
};
</script>"""


def test_reply_is_taken_when_the_message_selector_always_matches_one_element():
    [reply] = _run(_latest_only_page(), [PROMPT], ADAPTER_MESSAGE_SELECTOR=".latest", ADAPTER_SETTLE_SECONDS=2.0)
    assert reply.startswith("FILE: a.txt") and "STATUS: DONE" in reply


def test_copy_button_that_is_a_div_with_an_icon_font():
    [reply] = _run(_latest_only_page(copy_as_div=True), [PROMPT], ADAPTER_MESSAGE_SELECTOR=".latest",
                   ADAPTER_SETTLE_SECONDS=2.0)
    assert reply == "FILE: a.txt\n\nSTATUS: DONE (copied)"


def test_new_reply_rules():
    from app.adapters.generic.adapter import Adapter

    new = Adapter._is_new_reply
    sent = "You are the Coder. Write the file a.txt with hello in it, then report STATUS."
    assert new(2, "answer", 1, "old", sent)
    assert new(1, "answer", 1, "old", sent)  # same count, new text
    assert not new(1, "old", 1, "old", sent)
    assert not new(1, sent, 1, "old", sent)  # our own message
    assert not new(1, "chat: " + sent + " answer", 1, "old", sent)  # the whole chat with our message
    assert not new(0, "answer", 0, "", sent)
