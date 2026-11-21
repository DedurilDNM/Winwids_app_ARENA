"""Wait for GitHub Actions on the project's work branch and summarize the
result for the orchestrator (failed jobs/steps + the end of their logs).

The workflow itself (.github/workflows/*.yml) is written by an agent; the
server only reads results. Needs the token permission «Actions: Read».
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.connectors.github import GitHubError

logger = logging.getLogger(__name__)

PASSED, FAILED = "passed", "failed"
TERMINAL_OK = {"success", "skipped", "neutral"}


@dataclass
class CiResult:
    status: str  # passed | failed | no_workflow | no_access | not_started | timeout | stopped | error
    url: str = ""
    details: str = ""
    sha: str = ""

    def for_prompt(self) -> str:
        if self.status == PASSED:
            return "CI (GitHub Actions): PASSED for the latest commit."
        if self.status == FAILED:
            return f"CI (GitHub Actions): FAILED for the latest commit.\n{self.details}"
        if self.status == "no_workflow":
            return ("CI (GitHub Actions): there is NO workflow yet. Plan a task that adds "
                    ".github/workflows/ci.yml (install dependencies, build, run the tests on push).")
        return f"CI (GitHub Actions): {self.status}. {self.details}".strip()


def has_workflow(files: list[str]) -> bool:
    return any(f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml")) for f in files)


async def wait_for_ci(gh, repo: str, branch: str, *, timeout: float, poll: float, start_timeout: float,
                      should_continue=None) -> CiResult:
    try:
        if not has_workflow(await gh.list_tree(repo, branch)):
            return CiResult("no_workflow")
        if not await gh.actions_readable(repo):
            return CiResult("no_access", details="The GitHub token has no «Actions: Read» permission.")
        sha = await gh.get_branch_sha(repo, branch) or ""
        started = time.monotonic()
        while True:
            if should_continue is not None and not await should_continue():
                return CiResult("stopped", sha=sha)
            runs = await gh.workflow_runs(repo, sha)
            if runs and all(r["status"] == "completed" for r in runs):
                failed = [r for r in runs if (r["conclusion"] or "") not in TERMINAL_OK]
                if not failed:
                    return CiResult(PASSED, url=runs[0]["html_url"], sha=sha)
                return CiResult(FAILED, url=failed[0]["html_url"], sha=sha, details=await _failure_details(gh, repo, failed))
            elapsed = time.monotonic() - started
            if not runs and elapsed > start_timeout:
                return CiResult("not_started", sha=sha, details=(
                    "No workflow run appeared for the latest commit. Are GitHub Actions enabled for the "
                    "repository, and does the workflow run on push to every branch?"))
            if elapsed > timeout:
                return CiResult("timeout", url=runs[0]["html_url"] if runs else "", sha=sha,
                                details=f"CI did not finish within {int(timeout)}s.")
            await asyncio.sleep(poll)
    except GitHubError as exc:
        logger.warning("CI check failed: %s", exc.message)
        return CiResult("error", details=exc.message)


async def _failure_details(gh, repo: str, runs: list[dict]) -> str:
    parts = []
    for run in runs[:3]:
        jobs = await gh.run_jobs(repo, run["id"])
        bad = [j for j in jobs if (j["conclusion"] or "") not in TERMINAL_OK] or jobs[:1]
        for job in bad[:3]:
            tail = await gh.job_log_tail(repo, job["id"], max_chars=4000)
            steps = ", ".join(job["failed_steps"]) or "?"
            parts.append(f"Workflow «{run['name']}», job «{job['name']}» failed at step(s): {steps}\n"
                         f"--- end of the log ---\n{tail}")
    return "\n\n".join(parts)[:12000]
