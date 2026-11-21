"""End-to-end orchestration with fake agents and an in-memory GitHub:
plan -> agents reply with files -> the SERVER commits them -> PR."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.connectors.github import GitHubConnector
from app.main import app
from app.services import orchestrator
from app.services.agent_output import AgentOutputError, parse_agent_output
from tests.fake_github import FakeGitHub


# --------------------------------------------------------------- parser


def test_parser_markdown_and_rendered_and_nested_fences() -> None:
    out = parse_agent_output(
        "Here you go.\n\n### FILE: `src/app.py`\n```python\nprint('STATUS: DONE')\n```\n\n"
        "FILE: README.md\n````markdown\n# T\n```bash\nFILE: nope\n```\n````\n"
        "STATUS: DONE\nSUMMARY: app + readme\n"
    )
    assert out.status == "DONE" and out.summary == "app + readme"
    assert out.files == {"src/app.py": "print('STATUS: DONE')\n", "README.md": "# T\n```bash\nFILE: nope\n```\n"}
    rendered = parse_agent_output("FILE: a.txt\nhello\n\nSTATUS: BLOCKED\nSUMMARY: need a key")
    assert rendered.status == "BLOCKED" and rendered.files == {"a.txt": "hello\n"}


@pytest.mark.parametrize("reply, error", [
    ("just text", "no 'STATUS"),
    ("FILE: ../etc/passwd\nx\nSTATUS: DONE", "Invalid file path"),
    ("FILE: .git/config\nx\nSTATUS: DONE", "Invalid file path"),
    ("FILE: /abs\nx\nSTATUS: DONE", "Invalid file path"),
    ("STATUS: DONE\nSUMMARY: nothing", "no FILE blocks"),
    ("FILE: a\n\nSTATUS: DONE", "Empty file"),
])
def test_parser_rejects(reply: str, error: str) -> None:
    with pytest.raises(AgentOutputError, match=error):
        parse_agent_output(reply)


# --------------------------------------------------------------- fakes


class FakePage(str):
    """chat_ref string that also records being closed."""

    closed: list = []

    async def close(self):
        FakePage.closed.append(str(self))


class FakeAdapter:
    """Replies by chat: the orchestrator plans, agents return files."""

    def __init__(self, replies: dict[str, list[str]]) -> None:
        self.replies = replies  # chat_ref prefix -> queue of replies
        self.prompts: list[tuple[str, str]] = []
        self.on_attention = None
        self.escaped = 0

    async def open_chat(self, context, chat_ref):
        return FakePage(chat_ref)

    async def after_reply(self, page):
        self.escaped += 1

    async def wait_response(self, page, timeout=600):
        if "-review-" in page:  # orchestrator review: accept by default
            queue = self.replies.get(page)
            return queue.pop(0) if queue else '{"verdict": "done"}'
        return self.replies[page].pop(0)

    async def send_message(self, page, text):
        self.prompts.append((page, text))


class FakeBrowserManager:
    def get_context(self, account_id):
        return object()


class RecordingWS:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_to_project(self, project_id, event):
        self.events.append(event)


def _setup(github_repo: str = "octo/website", with_token: bool = True) -> dict:
    async def go() -> dict:
        from app.db import SessionLocal
        from app.models import Account, ConnectorCredential, Project, ProjectAssignment, User
        from app.security import encrypt_bytes, hash_password

        async with SessionLocal() as s:
            user = User(email=f"pipe{__import__("uuid").uuid4().hex}@example.com", hashed_password=hash_password("x" * 10))
            s.add(user)
            await s.flush()
            orch = Account(user_id=user.id, name="boss", default_role="Orchestrator", status="active", github_repos=[])
            coder = Account(user_id=user.id, name="coder", default_role="Coder", status="active", github_repos=[])
            reviewer = Account(user_id=user.id, name="rev", default_role="Reviewer", status="active", github_repos=[])
            s.add_all([orch, coder, reviewer])
            await s.flush()
            project = Project(user_id=user.id, name="Website", description="", task="Build a landing page",
                              github_repo=github_repo, orchestrator_account_id=orch.id, status="running")
            s.add(project)
            await s.flush()
            s.add_all([ProjectAssignment(project_id=project.id, account_id=coder.id, role="Coder"),
                       ProjectAssignment(project_id=project.id, account_id=reviewer.id, role="Tester")])
            if with_token:
                s.add(ConnectorCredential(user_id=user.id, kind="github", label="octo",
                                          encrypted_payload=encrypt_bytes(json.dumps({"token": "ghp_test_token_1234567890"}).encode())))
            await s.commit()
            return {"project": project.id, "orch": orch.id, "coder": coder.id, "reviewer": reviewer.id}

    with TestClient(app):  # runs init_db
        pass
    return asyncio.run(go())


def _run(ids: dict, adapter: FakeAdapter, gh: FakeGitHub) -> tuple[str, RecordingWS]:
    from app.db import SessionLocal

    ws = RecordingWS()
    status = asyncio.run(orchestrator.run_project_logic(
        ids["project"],
        session_factory=SessionLocal,
        adapter_factory=lambda: adapter,
        github_factory=lambda token: GitHubConnector(token=token, transport=gh.transport()),
        browser_manager=FakeBrowserManager(),
        ws_manager=ws,
    ))
    return status, ws


def _load(ids: dict):
    async def go():
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Project, Task

        async with SessionLocal() as s:
            project = await s.get(Project, ids["project"])
            tasks = (await s.scalars(select(Task).where(Task.project_id == ids["project"]).order_by(Task.id))).all()
            return project, tasks

    return asyncio.run(go())


def _plan(ids: dict) -> str:
    # The reviewer depends on the coder -> sequential (without depends_on they run in parallel).
    return json.dumps({"tasks": [
        {"id": "t1", "role": "Coder", "task": "Create index.html", "assign_to_account_id": ids["coder"]},
        {"id": "t2", "role": "Reviewer", "task": "Review index.html", "assign_to_account_id": ids["reviewer"],
         "depends_on": ["t1"]},
    ]})


# --------------------------------------------------------------- pipeline


def test_pipeline_commits_files_and_opens_pr() -> None:
    ids = _setup()
    p = ids["project"]
    gh = FakeGitHub()
    adapter = FakeAdapter({
        f"project-{p}": [_plan(ids), "## Summary\nLanding page done."],
        # first task: one malformed reply, then a valid one (format retry)
        f"project-{p}-task-": [],
    })

    # Task ids are only known after planning: route by prefix.
    replies = {
        "coder": ["Sorry, here is the code: <html></html>",
                  "FILE: index.html\n```html\n<h1>Hello</h1>\n```\nSTATUS: DONE\nSUMMARY: Added index.html"],
        "reviewer": ["FILE: index.html\n```html\n<h1>Hello, world</h1>\n```\n"
                     "FILE: reviews/index.md\n````markdown\n# Review\nFixed greeting.\n````\n"
                     "STATUS: DONE\nSUMMARY: Reviewed and fixed the greeting"],
    }
    original_wait = adapter.wait_response

    async def wait_response(page, timeout=600):
        if page.startswith(f"project-{p}-task-"):
            prompt = adapter.prompts[-1][1]
            who = "coder" if "You are the Coder" in prompt else "reviewer"
            assert "You are the Coder" in prompt or "You are the Tester" in prompt
            return replies[who].pop(0)
        return await original_wait(page, timeout)

    adapter.wait_response = wait_response
    status, ws = _run(ids, adapter, gh)
    assert status == "completed", _load(ids)[0].result

    branch = f"ai/project-{p}"
    files = gh.files(branch)
    assert files["index.html"] == "<h1>Hello, world</h1>\n"
    assert files["reviews/index.md"] == "# Review\nFixed greeting.\n"
    assert files["README.md"] == "# Website\n"  # untouched base content
    assert files[f"status/task-{_load(ids)[1][0].id}.md"].startswith("DONE")
    assert gh.files("main") == {"README.md": "# Website\n"}  # default branch never written
    log = gh.log(branch)
    assert log[0].startswith(f"task-{_load(ids)[1][1].id} [Tester]") and log[1].startswith(f"task-{_load(ids)[1][0].id} [Coder]")

    project, tasks = _load(ids)
    assert project.status == "completed" and project.pr_url == f"https://github.com/octo/website/pull/1"
    assert project.work_branch == branch and "Landing page done" in project.result
    assert [t.status for t in tasks] == ["done", "done"]
    assert tasks[0].summary == "Added index.html" and tasks[0].commit_sha
    assert gh.pulls[0]["head"] == branch and gh.pulls[0]["base"] == "main"

    agent_prompts = [t for page, t in adapter.prompts if page.startswith(f"project-{p}-task-")]
    assert len(agent_prompts) == 3  # coder, coder retry, reviewer
    assert "could not be processed" in agent_prompts[1] and "OUTPUT FORMAT" in agent_prompts[1]
    # the reviewer sees the coder's file content and summary
    assert "<h1>Hello</h1>" in agent_prompts[2] and "Added index.html" in agent_prompts[2]
    assert "README.md" in adapter.prompts[0][1]  # split prompt lists repo files
    assert f"account_id={ids['reviewer']} name='rev' role=Tester" in adapter.prompts[0][1]  # project role, not default
    assert [t.role for t in tasks] == ["Coder", "Tester"]
    types = [e["type"] for e in ws.events]
    assert "project_completed" in types and types.count("task_started") == 2


def test_blocked_agent_stops_project() -> None:
    ids = _setup()
    p = ids["project"]
    gh = FakeGitHub()
    adapter = FakeAdapter({f"project-{p}": [_plan(ids)]})
    adapter.wait_response = _by_prefix(adapter, p, "STATUS: BLOCKED\nSUMMARY: need the design mockups")
    status, _ = _run(ids, adapter, gh)
    project, tasks = _load(ids)
    assert status == "failed" and "need the design mockups" in project.result
    assert tasks[0].status == "blocked" and tasks[1].status == "pending"
    assert len(gh.log(f"ai/project-{p}")) == 1  # nothing committed


def test_no_github_token_fails_fast() -> None:
    ids = _setup(with_token=False)
    adapter = FakeAdapter({})
    status, _ = _run(ids, adapter, FakeGitHub())
    assert status == "failed" and "GitHub is not connected" in _load(ids)[0].result
    assert adapter.prompts == []  # no agent was bothered


def test_read_only_token_fails_fast() -> None:
    ids = _setup()
    adapter = FakeAdapter({})
    status, _ = _run(ids, adapter, FakeGitHub(can_push=False))
    assert status == "failed" and "cannot write" in _load(ids)[0].result
    assert adapter.prompts == []


def _by_prefix(adapter: FakeAdapter, project_id: int, agent_reply: str):
    original = adapter.wait_response

    async def wait_response(page, timeout=600):
        if page.startswith(f"project-{project_id}-task-"):
            return agent_reply
        return await original(page, timeout)

    return wait_response


def test_attention_events_reach_the_app_and_tabs_are_closed() -> None:
    from fastapi.testclient import TestClient as _TC

    from app.services.attention import list_attention

    ids = _setup()
    p = ids["project"]
    gh = FakeGitHub()
    adapter = FakeAdapter({f"project-{p}": [_plan(ids), "Summary"]})
    seen_during = []
    uid = _user_of(ids)

    async def wait_response(page, timeout=600):
        if page.startswith(f"project-{p}-task-"):
            await adapter.on_attention("captcha", True)  # e.g. a captcha shown after sending
            seen_during.append([i["kind"] for i in list_attention(uid)])
            await adapter.on_attention("captcha", False)
            return "FILE: a.txt\nhi\nSTATUS: DONE\nSUMMARY: ok"
        if "-review-" in page:
            return '{"verdict": "done"}'
        return adapter.replies[page].pop(0)

    adapter.wait_response = wait_response
    FakePage.closed = []
    status, ws = _run(ids, adapter, gh)
    assert status == "completed", _load(ids)[0].result
    types = [e["type"] for e in ws.events]
    assert types.count("attention_needed") == 2 and types.count("attention_resolved") == 2
    needed = next(e for e in ws.events if e["type"] == "attention_needed")
    assert needed["kind"] == "captcha" and needed["account_name"] in ("coder", "rev")
    assert seen_during == [["captcha"], ["captcha"]]
    assert list_attention(_user_of(ids)) == []
    assert len(FakePage.closed) == 5 and adapter.escaped == 5  # plan + 2 tasks + review + summary


def _user_of(ids: dict) -> int:
    async def go():
        from app.db import SessionLocal
        from app.models import Project

        async with SessionLocal() as s:
            return (await s.get(Project, ids["project"])).user_id

    return asyncio.run(go())
