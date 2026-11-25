"""The «aidd» MCP server Claude Code gets in every chat: the app's own tools.

- SendFile:   show a file of the project in the chat (a live prototype, an image, a table,
              a document to download).
- Screenshot: look at an HTML page of the project (desktop and phone) - the pictures come
              back to the model.

It runs inside the sandbox next to Claude Code (python3 -I, the standard library only) and
does the work through the server: POST AIDD_URL/<token>/aidd/<tool>. Protocol: MCP over
stdio, one JSON-RPC message per line.
"""

import json
import os
import sys
import urllib.request

URL = os.environ.get("AIDD_URL", "").rstrip("/")
VERSION = "2025-06-18"

TOOLS = [
    {"name": "SendFile",
     "description": "Show a finished file to the user in the chat: an HTML page opens as a live prototype, images "
                    "and PDF are shown, spreadsheets as a table, any file can be downloaded. Use it for deliverables "
                    "(prototypes, documents, reports, screenshots), not for every file you touch. The user does not "
                    "see files you do not send.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "the file, relative to the project folder"},
         "caption": {"type": "string", "description": "one line: what to look at"}}, "required": ["path"]}},
    {"name": "Screenshot",
     "description": "Open an HTML page of the project in a browser and look at it: returns screenshots (desktop "
                    "1440x900 and phone 390x844) as images you can see; they are also saved in screenshots/. Use it "
                    "to check a prototype or a web page you made, fix what looks wrong, and look again.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "an .html file of the project, e.g. prototype/index.html"},
         "viewport": {"type": "string", "enum": ["both", "desktop", "phone"]},
         "full_page": {"type": "boolean", "description": "the whole page (up to 3 screens), not one screen"}},
         "required": ["path"]}},
]


def call_server(tool, args):
    req = urllib.request.Request(f"{URL}/aidd/{tool.lower()}", data=json.dumps(args).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read() or b"{}")


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or VERSION,
                "capabilities": {"tools": {}}, "serverInfo": {"name": "aidd", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in ("SendFile", "Screenshot"):
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        try:
            out = call_server(name, args)
        except Exception as exc:  # the server is not reachable: say so, do not crash
            return {"content": [{"type": "text", "text": f"{name} failed: {exc}"}], "isError": True}
        content = [{"type": "text", "text": out.get("text", "")}]
        content += [{"type": "image", "data": i["data"], "mimeType": i["mime"]} for i in out.get("images", [])]
        return {"content": content, "isError": bool(out.get("is_error"))}
    if method == "ping":
        return {}
    if mid is None:
        return None  # a notification
    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        try:
            result = handle(msg)
            if mid is None:
                continue
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except LookupError as exc:
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {exc}"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
