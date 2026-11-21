"""CI (GitHub Actions) results: connector + wait_for_ci against the fake GitHub."""

from __future__ import annotations

import asyncio

from app.connectors.github import GitHubConnector
from app.services.ci import wait_for_ci
from tests.fake_github import FakeGitHub

WF = {".github/workflows/ci.yml": "on: push\njobs: {}\n"}


def _ci(gh: FakeGitHub, files: dict | None = None, **kw):
    conn = GitHubConnector(token=gh.token, transport=gh.transport())

    async def go():
        await conn.ensure_branch(gh.repo, "ai/p", "main")
        if files:
            await conn.commit_files(gh.repo, "ai/p", files, "add")
        return await wait_for_ci(conn, gh.repo, "ai/p", timeout=kw.get("timeout", 5), poll=0.01, start_timeout=0.2)

    return asyncio.run(go())


def test_no_workflow():
    assert _ci(FakeGitHub()).status == "no_workflow"


def test_passed():
    r = _ci(FakeGitHub(), {**WF, "app.py": "x = 1\n"})
    assert r.status == "passed" and r.url.endswith("/actions/runs/1")


def test_failed_with_log_tail():
    gh = FakeGitHub(ci_judge=lambda files: ("failure", "collecting...\nFAILED test_app.py::test_sum - assert 3 == 2"))
    r = _ci(gh, {**WF, "app.py": "x = 1\n"})
    assert r.status == "failed"
    assert "job «build» failed at step(s): Run tests" in r.details
    assert "assert 3 == 2" in r.details and "2026-01-01" not in r.details  # timestamps stripped
    assert "FAILED" in r.for_prompt()


def test_no_actions_permission():
    r = _ci(FakeGitHub(actions_read=False), {**WF})
    assert r.status == "no_access"


def test_workflow_write_needs_permission():
    import pytest

    from app.connectors.github import GitHubError

    with pytest.raises(GitHubError, match="workflow"):
        _ci(FakeGitHub(workflow_perm=False), {**WF})


def test_run_never_starts():
    gh = FakeGitHub()
    gh._maybe_start_ci = lambda sha: None  # Actions disabled in the repository
    assert _ci(gh, {**WF}).status == "not_started"
