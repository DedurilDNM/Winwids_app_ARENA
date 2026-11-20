"""Stand-in for the external AI service in e2e tests: a login form backed by a
cookie. Logs every page visit with whether the visitor was logged in."""

import http.server
import sys
from urllib.parse import parse_qs

LOG = sys.argv[2] if len(sys.argv) > 2 else "fake_service.log"


class Handler(http.server.BaseHTTPRequestHandler):
    def _user(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fake_session" and v:
                return v
        return None

    def do_GET(self):
        user = self._user()
        with open(LOG, "a") as f:
            f.write(f"VISIT user={user}\n")
        body = (
            f"<h1 id=state>Logged in as {user}</h1>" if user else
            "<h1 id=state>Please log in</h1><form method=post action=/login>"
            "<input name=user autofocus style='font-size:40px'></form>"
        )
        self.send_response(200)
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
