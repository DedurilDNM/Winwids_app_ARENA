"""Prompt templates for the orchestrator and the agents.

Pure functions (no I/O) so they are unit-testable.

Agents do NOT use any GitHub connector: they return complete files in their
chat reply following OUTPUT_CONTRACT, and the server commits them to the
project branch with the user's token. The contract is appended by the server
to every agent prompt, so the orchestrator never has to restate it.
No concrete AI service is named anywhere in these prompts.
"""

from __future__ import annotations

from typing import Any

ROLE_GUIDANCE = {
    "Architect": "Design the structure: modules, interfaces, data model. Deliver it as real files "
                 "(e.g. docs/ARCHITECTURE.md plus skeleton source files with signatures and docstrings).",
    "Coder": "Implement the task with working, complete code. Follow the existing structure and style of the repository.",
    "Reviewer": "Review the existing code for bugs, security and clarity. Fix what you find directly in the files, "
                "and write your findings to reviews/<short-name>.md.",
    "Tester": "Write automated tests for the existing code (and test fixtures if needed). "
              "Tests must be runnable with the project's standard test command.",
    "Integrator": "Make the pieces work together: wiring, configuration, build/run scripts and the README usage section.",
    "ImagePrompter": (
        "Write production-quality prompts for image-generation models, one file per image: "
        "prompts/<short-slug>.md. Each file has these sections: Purpose (where the image is used); "
        "Prompt (one dense English paragraph: subject and action, composition and framing, camera/lens and "
        "point of view, lighting, colour palette, materials and textures, environment, mood, art style or "
        "medium described by genre/era/technique - never by the name of a living artist); Negative prompt "
        "(what must not appear); Variations (2-3 alternative prompts: different angle, mood or style); "
        "Parameters (aspect ratio, suggested size, style strength / seed notes); Summary (2 lines in the "
        "language of the project task). Be specific and visual, avoid vague words like 'beautiful'. "
        "Keep text that must appear in the image short and in quotes."
    ),
    "Orchestrator": "Do the task as described.",
}

OUTPUT_CONTRACT = """OUTPUT FORMAT (mandatory - your reply is parsed by a program):
You cannot access the repository yourself. The server commits the files from your reply.
For every file you create or change, output its FULL final content (never a diff, never "..." placeholders):

FILE: path/relative/to/repo/root.ext
```
<entire file content>
```

Use a four-backtick fence (````) for files that themselves contain ``` (e.g. Markdown).
After the last file, end your reply with exactly:

STATUS: DONE
SUMMARY: <1-3 sentences: what you did>

If you cannot complete the task, reply with:

STATUS: BLOCKED
SUMMARY: <what is missing or what went wrong>

Do not write anything after SUMMARY."""


def build_split_prompt(
    *,
    project_name: str,
    description: str,
    task: str,
    github_repo: str,
    accounts: list[dict[str, Any]],
    repo_files: list[str] | None = None,
) -> str:
    """Ask the orchestrator to split the project task into per-role subtasks."""
    account_lines = "\n".join(
        f"- account_id={a['id']} name={a['name']!r} role={a['default_role']}"
        for a in accounts
    ) or "(no accounts available)"
    files = _file_list(repo_files)
    return f"""You are the ORCHESTRATOR of a software project. Split the project task into concrete subtasks for the available agent accounts.

Project: {project_name}
Repository: {github_repo}
Description:
{description or "(none)"}

Project task:
{task}

Current files in the repository:
{files}

Available agent accounts:
{account_lines}

Rules:
- Each subtask must be assigned to exactly one account from the list above (use its account_id). Prefer the account's own role.
- Use roles from: Architect, Coder, Reviewer, Tester, Integrator, ImagePrompter (writes prompts for image generation).
- 1-6 subtasks, in execution order. Later subtasks see the files produced by earlier ones.
- Each subtask text must be self-contained and specific: which files to create or change and what "done" means.
- Agents only return complete files; they cannot run commands. Do not ask them to run, deploy or install anything.

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
    project_task: str = "",
    repo_files: list[str] | None = None,
    context_files: dict[str, str] | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> str:
    """One agent's subtask: role guidance + repository context + output contract."""
    guidance = ROLE_GUIDANCE.get(role, ROLE_GUIDANCE["Coder"])
    done_before = "\n".join(
        f"- task {t['id']} [{t['role']}]: {t.get('summary') or t['task'][:200]}" for t in (previous or [])
    ) or "(you are the first)"
    context = "\n\n".join(
        f"FILE: {path}\n````\n{content.rstrip()}\n````" for path, content in (context_files or {}).items()
    ) or "(no file contents provided)"
    return f"""You are the {role} on the software project "{project_name}" (repository {github_repo}, branch {branch}).

Overall project goal:
{project_task or "(see your task)"}

YOUR TASK (task {task_id}):
{task_text}

How to work as the {role}: {guidance}

Work already done by other agents:
{done_before}

All files currently in the repository:
{_file_list(repo_files)}

Contents of the relevant files:
{context}

{OUTPUT_CONTRACT}
"""


def build_fix_prompt(error: str) -> str:
    """Ask the agent to resend its answer in the required format."""
    return f"""Your previous reply could not be processed: {error}

Send the complete answer again, strictly in the required format.

{OUTPUT_CONTRACT}
"""


def build_final_prompt(
    *,
    project_name: str,
    github_repo: str,
    completed: list[dict[str, Any]],
    branch: str = "",
) -> str:
    """Ask the orchestrator for the final summary of the finished project."""
    lines = "\n".join(
        f"- task {t['id']} [{t['role']}] commit {str(t.get('commit') or '-')[:10]}: "
        f"{t.get('summary') or t['task'][:200]} (files: {', '.join(t.get('files') or []) or '-'})"
        for t in completed
    ) or "(no tasks)"
    return f"""You are the ORCHESTRATOR of project "{project_name}" (repository {github_repo}).

All agent tasks are complete; their files are committed on branch {branch or "(project branch)"} and a pull request will be opened by the server:
{lines}

Write the final project summary for the user:
- what was built or changed,
- how to run or use it (based on the files),
- anything left unfinished or needing human attention.

Be concise and factual. Use markdown. Reply in the language of the project task.
"""


def _file_list(paths: list[str] | None, limit: int = 300) -> str:
    if not paths:
        return "(empty repository or unknown)"
    shown = "\n".join(f"- {p}" for p in paths[:limit])
    more = f"\n... and {len(paths) - limit} more" if len(paths) > limit else ""
    return shown + more
