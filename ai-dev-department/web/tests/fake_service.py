"""Stand-in for the external AI service in e2e tests: a login form backed by a
cookie. Logs every page visit with whether the visitor was logged in."""

import http.server
import sys
from urllib.parse import parse_qs

LOG = sys.argv[2] if len(sys.argv) > 2 else "fake_service.log"


CHAT = """
<style>.msg p { white-space: pre-wrap; }</style>
<div id=messages></div>
<textarea id=box aria-label="Message" placeholder="Ask anything"></textarea>
<button id=send aria-label="Send message">Send</button>
<button id=mode role=combobox aria-label="Mode">Chat</button>
<script>
let box = document.getElementById("box");
const send = document.getElementById("send"), messages = document.getElementById("messages");
if (location.search.includes("editor=ce")) {
  // ProseMirror-like rich editor: accepts text only through its paste/keyboard
  // handlers; the old textarea stays in the DOM but hidden (selector trap).
  const ce = document.createElement("div");
  ce.className = "ProseMirror"; ce.contentEditable = "true";
  ce.style.cssText = "min-height:40px;border:1px solid #999;white-space:pre-wrap";
  ce.addEventListener("paste", (e) => {
    e.preventDefault();
    ce.innerText = (ce.innerText || "") + e.clipboardData.getData("text/plain");
  });
  box.style.display = "none";
  box.after(ce);
  box = ce;
}
const val = () => (box.value !== undefined ? box.value : box.innerText) || "";
const clear = () => { if (box.value !== undefined) box.value = ""; else box.innerText = ""; };
function add(role, text) {
  const d = document.createElement("div"); d.className = "msg msg-" + role; d.dataset.role = role;
  const p = document.createElement("p"); p.textContent = text; d.append(p); messages.append(d);
}
const mode = document.getElementById("mode");
mode.addEventListener("click", () => {
  const list = document.createElement("div"); list.setAttribute("role", "listbox");
  for (const name of ["Chat", "Agent"]) {
    const o = document.createElement("div"); o.setAttribute("role", "option"); o.textContent = name;
    o.addEventListener("click", () => { mode.textContent = name; list.remove(); });
    list.append(o);
  }
  document.body.append(list);
});
box.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send.click(); } });
send.addEventListener("click", () => {
  // ?terms=1: a one-time agreement pop-up on the first send (blocks the page)
  if (location.search.includes("terms=1") && !sessionStorage.getItem("terms_ok")) {
    const dlg = document.createElement("div");
    dlg.setAttribute("role", "dialog");
    dlg.style.cssText = "position:fixed;inset:0;background:#fff;z-index:9";
    const ok = document.createElement("button"); ok.dataset.testid = "accept-terms"; ok.textContent = "Agree";
    const no = document.createElement("button"); no.textContent = "Cancel";
    ok.onclick = () => { sessionStorage.setItem("terms_ok", "1"); dlg.remove(); };
    dlg.append(ok, no); document.body.append(dlg);
    return;
  }
  const t = val().trim(); if (!t) return; clear();
  add("user", t); messages.setAttribute("aria-busy", "true");
  const stop = document.createElement("button"); stop.setAttribute("aria-label", "Stop generating"); stop.textContent = "Stop";
  document.body.append(stop);
  setTimeout(() => {
    const w = t.match(/only the word (\\S+)/);
    const f = t.match(/(FILE: [\\s\\S]*SUMMARY: \\w+)/);
    // ?agent=1: answers only in Agent mode (like a service with a mode switch)
    const needAgent = location.search.includes("agent=1") && mode.textContent !== "Agent";
    add("assistant", needAgent ? "Switch to Agent mode" : w ? w[1] : f ? f[1] : "ok"); messages.setAttribute("aria-busy", "false"); stop.remove();
  }, 2000);
});
</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _user(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fake_session" and v:
                return v
        return None

    def do_GET(self):
        from urllib.parse import parse_qs as _pq, urlparse as _up

        auto = _pq(_up(self.path).query).get("as", [None])[0]  # ?as=<user>: log in on first visit
        user = self._user() or auto
        with open(LOG, "a") as f:
            f.write(f"VISIT user={user}\n")
        body = (
            f"<h1 id=state>Logged in as {user}</h1>" + CHAT if user else
            "<h1 id=state>Please log in</h1><form method=post action=/login>"
            "<input name=user autofocus style='font-size:40px'></form>"
        )
        self.send_response(200)
        if auto and not self._user():
            self.send_header("Set-Cookie", f"fake_session={auto}; Max-Age=86400; Path=/")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<!doctype html><title>Fake service</title>{body}".encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        user = parse_qs(self.rfile.read(length).decode()).get("user", [""])[0]
        self.send_response(303)
        self.send_header("Set-Cookie", f"fake_session={user}; Max-Age=86400; Path=/")
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
