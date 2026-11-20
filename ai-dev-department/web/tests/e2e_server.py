"""Run the real API for e2e tests with Chromium standing in for Camoufox.

Everything else is real: Xvfb, x11vnc, websockify, noVNC, storage_state
encryption and restore. Usage (from server/): python ../web/tests/e2e_server.py
Env: E2E_CHROMIUM=/path/to/chrome (Playwright's Chromium build).
"""

import os
import sys

sys.path.insert(0, os.getcwd())


from app.services import browser_manager as bm  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_server_patch import ChromiumAsCamoufox  # noqa: E402


bm.AsyncCamoufox = ChromiumAsCamoufox

if os.environ.get("E2E_FAKE_GITHUB"):
    # In-memory GitHub (same fake as the server unit tests).
    from app.api import github as github_api
    from app.connectors.github import GitHubConnector
    from app.services import orchestrator
    from tests.fake_github import FakeGitHub

    fake_github = FakeGitHub()
    github_api.connector_for = lambda token: GitHubConnector(token=token, transport=fake_github.transport())
    orchestrator._default_github = github_api.connector_for

if os.environ.get("E2E_FAKE_AGENTS"):
    # Scripted orchestrator + agents (the real adapter is tuned against the
    # real service separately). Small delays make live progress visible.
    import asyncio
    import json
    import re

    from app.services import orchestrator as orch_module

    class ScriptedAdapter:
        def __init__(self):
            self.calls = {}

        async def open_chat(self, context, chat_ref):
            return chat_ref

        async def is_alive(self, context):
            return True

        async def send_message(self, page, text):
            self.calls[page] = text

        async def wait_response(self, page, timeout=600):
            await asyncio.sleep(1.5)
            prompt = self.calls[page]
            if "-task-" not in page:
                if "Split the project task" in prompt:
                    ids = re.findall(r"account_id=(\d+) name='[^']*' role=(\w+)", prompt)
                    return json.dumps([{"role": role if role != "Orchestrator" else "Coder",
                                        "task": f"Part {n + 1} of the project", "assign_to_account_id": int(i)}
                                       for n, (i, role) in enumerate(ids)])
                return "## Итог\nЛендинг готов: index.html и ревью."
            role = re.search(r"You are the (\w+)", prompt).group(1)
            task_id = re.search(r"YOUR TASK \(task (\d+)\)", prompt).group(1)
            return (f"FILE: src/part-{task_id}.html\n```html\n<p>{role} work for task {task_id}</p>\n```\n"
                    f"STATUS: DONE\nSUMMARY: {role} finished task {task_id}")

    orch_module.load_adapter = lambda: ScriptedAdapter()

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
