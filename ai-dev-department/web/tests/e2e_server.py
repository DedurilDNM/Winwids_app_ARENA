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
            prompt = self.calls[page]
            accounts = re.findall(r"account_id=(\d+) name='([^']*)' role=(\w+)", prompt)
            if "Split the project task" in prompt:
                await asyncio.sleep(1)
                arch = next((i for i, _, r in accounts if r == "Architect"), None)
                tasks = []
                if arch:
                    tasks.append({"id": "t1", "role": "Architect", "task": "Write docs/CONTRACTS.md",
                                  "assign_to_account_id": int(arch), "scope": ["docs/"]})
                for n, (i, name, role) in enumerate([a for a in accounts if a[2] != "Architect"], start=2):
                    tasks.append({"id": f"t{n}", "role": "Coder" if role == "Orchestrator" else role,
                                  "task": f"Build module {n}", "assign_to_account_id": int(i),
                                  "depends_on": ["t1"] if arch else [], "scope": [f"src/m{n}/"]})
                return json.dumps({"tasks": tasks})
            if "Review round 1" in prompt:
                await asyncio.sleep(1)
                coder = next(i for i, _, r in accounts if r == "Coder")
                return json.dumps({"verdict": "continue", "answers": [{"to": "all", "text": "Good, one fix needed"}],
                                   "tasks": [{"id": "f1", "role": "Coder", "task": "Polish module 2",
                                              "assign_to_account_id": int(coder), "scope": ["src/m2/"]}]})
            if "Review round" in prompt:
                return '{"verdict": "done"}'
            if "All agent tasks are complete" in prompt:
                return "## Итог\nЛендинг готов: модули и ревью."
            await asyncio.sleep(4)  # long enough to see agents working in parallel
            role = re.search(r"You are the (\w+)", prompt).group(1)
            task_id = re.search(r"YOUR TASK \(task (\d+)\)", prompt).group(1)
            area = re.search(r"you may only write these paths: (\S+)", prompt).group(1).rstrip(",")
            path = f"{area}part-{task_id}.html" if area.endswith("/") else area
            msg = "MESSAGE to Coder: follow docs/CONTRACTS.md\n" if role == "Architect" else ""
            return (f"{msg}FILE: {path}\n```html\n<p>{role} work for task {task_id}</p>\n```\n"
                    f"STATUS: DONE\nSUMMARY: {role} finished task {task_id}")

    orch_module.load_adapter = lambda: ScriptedAdapter()

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
