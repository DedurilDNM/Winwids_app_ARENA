"""«Big department»: dependency graph, parallel agents with separate areas,
messages/questions between agents, orchestrator review rounds."""

from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi.testclient import TestClient

from app.connectors.github import GitHubConnector
from app.main import app
from app.services import orchestrator
from tests.fake_github import FakeGitHub
from tests.test_pipeline import FakeBrowserManager, RecordingWS


def _setup_team(n_coders: int = 3, max_rounds: int = 3) -> dict:
    async def go() -> dict:
        from app.db import SessionLocal
        from app.models import Account, ConnectorCredential, Project, ProjectAssignment, User
        from app.security import encrypt_bytes, hash_password

        async with SessionLocal() as s:
            user = User(email=f"dept{__import__("uuid").uuid4().hex}@example.com", hashed_password=hash_password("x" * 10))
            s.add(user)
            await s.flush()
            mk = lambda name, role: Account(user_id=user.id, name=name, default_role=role, status="active", github_repos=[])  # noqa: E731
            boss, arch, integ = mk("boss", "Orchestrator"), mk("arch", "Architect"), mk("integ", "Integrator")
            coders = [mk(f"coder{i}", "Coder") for i in range(1, n_coders + 1)]
            s.add_all([boss, arch, integ, *coders])
            await s.flush()
            project = Project(user_id=user.id, name="Shop", description="", task="Build a shop",
                              github_repo="octo/website", orchestrator_account_id=boss.id, status="running",
                              max_rounds=max_rounds)
            s.add(project)
            await s.flush()
            s.add_all([ProjectAssignment(project_id=project.id, account_id=a.id, role=a.default_role)
                       for a in (arch, integ, *coders)])
            s.add(ConnectorCredential(user_id=user.id, kind="github", label="octo",
                                      encrypted_payload=encrypt_bytes(json.dumps({"token": "ghp_test_token_1234567890"}).encode())))
            await s.commit()
            return {"project": project.id, "user": user.id, "boss": boss.id, "arch": arch.id, "integ": integ.id,
                    "coders": [c.id for c in coders]}

    with TestClient(app):
        pass
    return asyncio.run(go())


def _team_plan(ids: dict) -> str:
    tasks = [{"id": "t1", "role": "Architect", "task": "Write the contract", "assign_to_account_id": ids["arch"],
              "scope": ["docs/"]}]
    for n, cid in enumerate(ids["coders"], start=2):
        tasks.append({"id": f"t{n}", "role": "Coder", "task": f"Module {n}", "assign_to_account_id": cid,
                      "depends_on": ["t1"], "scope": [f"src/m{n}/"]})
    tasks.append({"id": "tI", "role": "Integrator", "task": "Wire modules", "assign_to_account_id": ids["integ"],
                  "depends_on": [t["id"] for t in tasks if t["role"] == "Coder"], "scope": ["src/main.py", "README.md"]})
    return json.dumps({"tasks": tasks})


class TeamAdapter:
    """Each agent writes one file in its area; timings recorded per task."""

    def __init__(self, ids, *, review, agent_reply=None, delay=0.3):
        self.ids, self.review, self.agent_reply, self.delay = ids, review, agent_reply, delay
        self.prompts: dict[str, list[str]] = {}
        self.timeline: dict[str, tuple[float, float]] = {}
        self.reviews = 0
        self.on_attention = self.should_continue = None

    async def open_chat(self, context, chat_ref):
        return chat_ref

    async def send_message(self, page, text):
        self.prompts.setdefault(page, []).append(text)

    async def wait_response(self, page, timeout=600):
        prompt = self.prompts[page][-1]
        p = self.ids["project"]
        if page == f"project-{p}":
            return _team_plan(self.ids) if "Split the project task" in prompt else "## Summary\nShop built."
        if "-review-" in page:
            self.reviews += 1
            return self.review(self.reviews, prompt)
        first = self.prompts[page][0]  # follow-ups in the same chat are short
        task_id = re.search(r"YOUR TASK \(task (\d+)\)", first).group(1)
        role = re.search(r"You are the (\w+)", first).group(1)
        area = re.search(r"you may only write these paths: (\S+)", first).group(1).rstrip(",")
        start = time.monotonic()
        await asyncio.sleep(self.delay)
        self.timeline[task_id] = (start, time.monotonic())
        if self.agent_reply:
            custom = self.agent_reply(role, task_id, area, first)
            if custom:
                return custom
        path = area if not area.endswith("/") else f"{area}part{task_id}.py"
        return f"FILE: {path}\n```\n# {role} task {task_id}\n```\nSTATUS: DONE\nSUMMARY: {role} did {task_id}"


def _run(ids, adapter, gh):
    from app.db import SessionLocal

    ws = RecordingWS()
    status = asyncio.run(orchestrator.run_project_logic(
        ids["project"], session_factory=SessionLocal, adapter_factory=lambda: adapter,
        github_factory=lambda token: GitHubConnector(token=token, transport=gh.transport()),
        browser_manager=FakeBrowserManager(), ws_manager=ws,
    ))
    return status, ws


def _tasks(ids):
    async def go():
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import AgentMessage, Project, Task

        async with SessionLocal() as s:
            project = await s.get(Project, ids["project"])
            tasks = (await s.scalars(select(Task).where(Task.project_id == ids["project"]).order_by(Task.id))).all()
            msgs = (await s.scalars(select(AgentMessage).where(AgentMessage.project_id == ids["project"]))).all()
            return project, tasks, msgs

    return asyncio.run(go())


def _accept(n, prompt):
    return '{"verdict": "done"}'


def test_three_coders_work_in_parallel_then_integrator_gets_their_code() -> None:
    ids = _setup_team()
    gh = FakeGitHub()
    adapter = TeamAdapter(ids, review=_accept)
    status, ws = _run(ids, adapter, gh)
    project, tasks, _ = _tasks(ids)
    assert status == "completed", project.result

    by_role = {}
    for t in tasks:
        by_role.setdefault(t.role, []).append(t)
    arch, coders, integ = by_role["Architect"][0], by_role["Coder"], by_role["Integrator"][0]
    tl = {int(k): v for k, v in adapter.timeline.items()}
    # coders start after the architect, overlap each other, the integrator waits for all of them
    assert all(tl[c.id][0] >= tl[arch.id][1] for c in coders)
    latest_start = max(tl[c.id][0] for c in coders)
    earliest_end = min(tl[c.id][1] for c in coders)
    assert latest_start < earliest_end, "coders did not run in parallel"
    assert tl[integ.id][0] >= max(tl[c.id][1] for c in coders)
    assert integ.depends_on == sorted(c.id for c in coders) and coders[0].scope == ["src/m2/"]

    files = gh.files(f"ai/project-{ids['project']}")
    for c in coders:
        assert any(f.startswith(c.scope[0]) for f in files)
    integ_prompt = adapter.prompts[f"project-{ids['project']}-task-{integ.id}"][0]
    assert all(f"FILE: src/m{n}/part" in integ_prompt for n in (2, 3, 4)), "integrator must see the coders' code"
    assert "Agents working in parallel with you" in adapter.prompts[f"project-{ids['project']}-task-{coders[0].id}"][0]
    assert len(gh.log(f"ai/project-{ids['project']}")) == 1 + 5  # init + 5 serialized commits
    assert gh.pulls and project.pr_url


def test_writing_outside_the_area_is_sent_back_for_a_fix() -> None:
    ids = _setup_team(n_coders=1)
    tries = {}

    def reply(role, task_id, area, prompt):
        if role == "Coder":
            tries[task_id] = tries.get(task_id, 0) + 1
            if tries[task_id] == 1:
                return "FILE: docs/CONTRACTS.md\n```\nhacked\n```\nSTATUS: DONE\nSUMMARY: x"
        return None

    adapter = TeamAdapter(ids, review=_accept, agent_reply=reply, delay=0)
    gh = FakeGitHub()
    status, _ = _run(ids, adapter, gh)
    assert status == "completed"
    coder_task = next(t for t in _tasks(ids)[1] if t.role == "Coder")
    retry = adapter.prompts[f"project-{ids['project']}-task-{coder_task.id}"][1]
    assert "outside your area" in retry
    assert "hacked" not in gh.files(f"ai/project-{ids['project']}").get("docs/CONTRACTS.md", "")


def test_messages_questions_and_review_rounds() -> None:
    ids = _setup_team(n_coders=2)

    def reply(role, task_id, area, prompt):
        if role == "Architect":
            return ("MESSAGE to Coder: export run() from every module\n"
                    "FILE: docs/CONTRACTS.md\n```\nrun()\n```\nSTATUS: DONE\nSUMMARY: contract")
        if role == "Coder" and "Module 2" in prompt:
            return (f"QUESTION: which currency?\nFILE: {area}a.py\n```\ndef run(): pass\n```\n"
                    "STATUS: DONE\nSUMMARY: module 2")
        return None

    def review(n, prompt):
        if n == 1:
            assert "from coder1: which currency?" in prompt
            return json.dumps({"verdict": "continue",
                               "answers": [{"to": "coder1", "text": "Use EUR"}],
                               "tasks": [{"id": "f1", "role": "Coder", "task": "Fix currency",
                                          "assign_to_account_id": ids["coders"][0], "scope": ["src/m2/"]}]})
        return '{"verdict": "done"}'

    adapter = TeamAdapter(ids, review=review, agent_reply=reply, delay=0)
    status, ws = _run(ids, adapter, FakeGitHub())
    project, tasks, msgs = _tasks(ids)
    assert status == "completed", project.result
    p = ids["project"]
    coder_tasks = [t for t in tasks if t.role == "Coder"]
    for t in coder_tasks[:2]:  # both coders got the architect's message
        assert "from arch: export run() from every module" in adapter.prompts[f"project-{p}-task-{t.id}"][0]
    fix = next(t for t in tasks if t.round == 1)
    assert fix.task == "Fix currency" and fix.status == "done"
    assert "from orchestrator: Use EUR" in adapter.prompts[f"project-{p}-task-{fix.id}"][0]
    assert {m.kind for m in msgs} == {"message", "question", "answer"}
    types = [e["type"] for e in ws.events]
    assert "agent_message" in types and "review_round" in types and "review_done" in types
    assert adapter.reviews == 2


def test_review_rounds_are_limited() -> None:
    ids = _setup_team(n_coders=1, max_rounds=2)

    def always_more(n, prompt):
        return json.dumps({"verdict": "continue", "tasks": [
            {"id": "x", "role": "Coder", "task": f"More work {n}", "assign_to_account_id": ids["coders"][0],
             "scope": ["src/m2/"]}]})

    adapter = TeamAdapter(ids, review=always_more, delay=0)
    status, _ = _run(ids, adapter, FakeGitHub())
    assert status == "completed"
    assert adapter.reviews == 2  # max_rounds=2: two reviews, then the summary
    assert max(t.round for t in _tasks(ids)[1]) == 2


def test_plan_with_a_cycle_is_rejected() -> None:
    import pytest

    from app.services.orchestrator import parse_task_plan

    class A:
        def __init__(self, i, r):
            self.account_id, self.role = i, r

    plan = json.dumps({"tasks": [
        {"id": "a", "role": "Coder", "task": "x", "assign_to_account_id": 1, "depends_on": ["b"]},
        {"id": "b", "role": "Coder", "task": "y", "assign_to_account_id": 1, "depends_on": ["a"]}]})
    with pytest.raises(ValueError, match="circular"):
        parse_task_plan(plan, assignments=[A(1, "Coder")], orchestrator_account_id=None)
    legacy = parse_task_plan('[{"role": "Coder", "task": "x", "assign_to_account_id": 1}]',
                             assignments=[A(1, "Coder")], orchestrator_account_id=None)
    assert legacy[0].key == "t1" and legacy[0].depends_on == [] and legacy[0].scope == []


def _fast_ci(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "CI_POLL_SECONDS", 0.01)
    monkeypatch.setattr(s, "CI_START_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(s, "CI_TIMEOUT_SECONDS", 10)


def _ci_team_plan(ids):
    return json.dumps({"tasks": [
        {"id": "t1", "role": "Coder", "task": "Write the sum module", "assign_to_account_id": ids["coders"][0],
         "scope": ["src/"]},
        {"id": "t2", "role": "Integrator", "task": "Add CI", "assign_to_account_id": ids["integ"],
         "depends_on": ["t1"], "scope": [".github/workflows/", "README.md"]},
    ]})


def test_ci_failure_goes_back_to_the_orchestrator_and_gets_fixed(monkeypatch):
    _fast_ci(monkeypatch)
    ids = _setup_team(n_coders=1)
    seen_reviews = []

    def judge(files):
        if "BUG" in files.get("src/sum.py", ""):
            return "failure", "collected 1 item\nFAILED tests/test_sum.py::test_sum - assert 3 == 2"
        return "success", "1 passed"

    def reply(role, task_id, area, prompt):
        if role == "Coder":
            body = "def add(a, b):\n    return a + b\n" if "Fix" in prompt else "def add(a, b):\n    return a + b + 1  # BUG\n"
            return f"FILE: src/sum.py\n```python\n{body}```\nSTATUS: DONE\nSUMMARY: sum module"
        if role == "Integrator":
            return ("FILE: .github/workflows/ci.yml\n```yaml\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n```\n"
                    "STATUS: DONE\nSUMMARY: CI added")
        return None

    def review(n, prompt):
        seen_reviews.append(prompt)
        if n == 1:
            assert "CI (GitHub Actions): FAILED" in prompt and "assert 3 == 2" in prompt
            return json.dumps({"verdict": "continue", "tasks": [
                {"id": "f1", "role": "Coder", "task": "Fix add() so test_sum passes",
                 "assign_to_account_id": ids["coders"][0], "scope": ["src/"]}]})
        assert "CI (GitHub Actions): PASSED" in prompt
        return '{"verdict": "done"}'

    adapter = TeamAdapter(ids, review=review, agent_reply=reply, delay=0)
    original = adapter.wait_response

    async def wait_response(page, timeout=600):  # this test's own plan
        if page == f"project-{ids['project']}" and "Split the project task" in adapter.prompts[page][-1]:
            assert "writes .github/workflows/ci.yml" in adapter.prompts[page][-1]  # CI rule in the plan prompt
            return _ci_team_plan(ids)
        return await original(page, timeout)

    adapter.wait_response = wait_response
    gh = FakeGitHub(ci_judge=judge)
    status, ws = _run(ids, adapter, gh)
    project, tasks, _ = _tasks(ids)
    assert status == "completed", project.result
    assert len(seen_reviews) == 2
    assert project.ci_status == "passed" and project.ci_url.endswith("/actions/runs/" + str(len(gh.runs)))
    assert "✅ CI passed" in project.result and "✅ CI passed" in gh.pulls[0]["body"]
    assert [e["ci_status"] for e in ws.events if e["type"] == "ci_result"] == ["failed", "passed"]
    assert "BUG" not in gh.files(f"ai/project-{ids['project']}")["src/sum.py"]


def test_missing_workflows_permission_is_explained(monkeypatch):
    _fast_ci(monkeypatch)
    ids = _setup_team(n_coders=1)

    def reply(role, task_id, area, prompt):
        if role == "Integrator":
            return ("FILE: .github/workflows/ci.yml\n```yaml\non: push\n```\nSTATUS: DONE\nSUMMARY: CI")
        return None

    adapter = TeamAdapter(ids, review=_accept, agent_reply=reply, delay=0)
    original = adapter.wait_response

    async def wait_response(page, timeout=600):
        if page == f"project-{ids['project']}" and "Split the project task" in adapter.prompts[page][-1]:
            return _ci_team_plan(ids)
        return await original(page, timeout)

    adapter.wait_response = wait_response
    status, _ = _run(ids, adapter, FakeGitHub(workflow_perm=False))
    project = _tasks(ids)[0]
    assert status == "failed" and "Workflows: Read and write" in project.result
