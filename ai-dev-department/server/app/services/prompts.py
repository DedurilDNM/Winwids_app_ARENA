"""Prompt templates for the orchestrator and the agents.

Pure functions (no I/O) so they are unit-testable. The agents work through
the external service's own GitHub connector - we only instruct them here.
No concrete AI service is named anywhere in these prompts.
"""

from __future__ import annotations

from typing import Any


def build_split_prompt(
    *,
    project_name: str,
    description: str,
    task: str,
    github_repo: str,
    accounts: list[dict[str, Any]],
) -> str:
    """Ask the orchestrator to split the project task into per-role subtasks."""
    account_lines = "\n".join(
        f"- account_id={a['id']} name={a['name']!r} role={a['default_role']}"
        for a in accounts
    ) or "(no accounts available)"
    return f"""You are the ORCHESTRATOR of a software project. Split the project task into concrete subtasks for the available agent accounts.

Project: {project_name}
Repository: {github_repo}
Description:
{description or "(none)"}

Project task:
{task}

Available agent accounts:
{account_lines}

Rules:
- Each subtask must be assigned to exactly one account from the list above (use its account_id).
- Use roles from: Architect, Coder, Reviewer, Tester, Integrator.
- Keep subtasks small and independently verifiable (1-5 subtasks total).
- Each agent will work in the GitHub repository using its own GitHub connector, on its own branch, and will report completion by writing a status file.

Reply with ONLY a JSON array, no other text, in exactly this format:
[{{"role": "Coder", "task": "what to do", "assign_to_account_id": 1}}]
"""


def build_agent_prompt(
    *,
    project_name: str,
    github_repo: str,
    role: str,
    task_text: str,
    task_id: int,
    branch: str,
) -> str:
    """Instruct one agent to do its subtask in the repo and report via a status file."""
    status_path = f"status/task-{task_id}.md"
    return f"""You are the {role} agent on project "{project_name}".

Repository: {github_repo}
Your task:
{task_text}

Instructions:
1. Use your GitHub connector to work in the repository {github_repo}.
2. Create and switch to a new branch named exactly: {branch}
3. Implement the task on that branch and commit your changes.
4. When finished, create a file at exactly this path on the same branch: {status_path}
   The FIRST line of that file must be exactly one of:
   DONE
   BLOCKED
   Followed by a short explanation (what was done, or why you are blocked).
5. Do not merge anything into the default branch. Do not touch other task branches.

Start now.
"""


def build_final_prompt(
    *,
    project_name: str,
    github_repo: str,
    completed: list[dict[str, Any]],
) -> str:
    """Ask the orchestrator for the final summary (PR link + commits)."""
    lines = "\n".join(
        f"- task {t['id']} [{t['role']}] branch={t.get('branch') or '-'}: {t['task'][:200]}"
        for t in completed
    ) or "(no tasks)"
    return f"""You are the ORCHESTRATOR of project "{project_name}" (repository {github_repo}).

All agent tasks are complete:
{lines}

Write the final project summary for the user. Include:
- what was built/changed (per task),
- the pull request link (create the PR from the task branches if needed, or state why not),
- the commit links (or commit hashes) for each task branch,
- anything left unfinished or needing human attention.

Be concise and factual. Use markdown.
"""
