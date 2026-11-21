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
For every file you create or change, output its FULL final content (never a diff, never "..." or
"rest of the code" placeholders - the server rejects unfinished files). Keep each file under ~300 lines;
split bigger code into several files:

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
    ci: bool = False,
) -> str:
    """Ask the orchestrator to split the project task into per-role subtasks."""
    account_lines = "\n".join(
        f"- account_id={a['id']} name={a['name']!r} role={a['default_role']}"
        for a in accounts
    ) or "(no accounts available)"
    files = _file_list(repo_files)
    ci_rule = (
        "- CI: the server runs GitHub Actions after every round and shows you the result. Unless the repository\n"
        "  already has one, plan a subtask (Integrator or Tester) whose scope includes \".github/workflows/\" and that\n"
        "  writes .github/workflows/ci.yml: on push to any branch; set up the language; install dependencies; build;\n"
        "  run the tests (and lint if cheap). It must pass on a clean runner. Plan tests the CI will run.\n"
    ) if ci else ""
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

You lead a team that works IN PARALLEL. Rules:
- Assign every subtask to exactly one account from the list (its account_id). Prefer the account's role.
- Roles: Architect, Coder, Reviewer, Tester, Integrator, ImagePrompter (writes prompts for image generation).
- Parallel work: subtasks without dependencies run at the same time; one account does one subtask at a time.
  If several accounts share a role (e.g. 3 Coders), split that work between them into separate parts.
- No conflicts: give every subtask an "scope" - the folders/files it alone may write (e.g. ["src/api/"],
  ["src/ui/", "assets/"], ["README.md"]). Scopes of subtasks that can run at the same time must not overlap.
- Order with "depends_on": usually an Architect first writes docs/CONTRACTS.md (modules, interfaces, data
  formats) that the others follow; the parts are built in parallel; an Integrator (or a Coder) then wires the
  parts together (entry point, configuration, README); a Tester/Reviewer comes after what they check.
- Each subtask text must be self-contained: what to build, which interfaces from the contract to follow,
  what "done" means. 2-8 subtasks. Agents only return files; they cannot run commands.
{ci_rule}
Reply with ONLY JSON, no other text, in exactly this format:
{{"tasks": [
  {{"id": "t1", "role": "Architect", "task": "...", "assign_to_account_id": 1, "depends_on": [], "scope": ["docs/"]}},
  {{"id": "t2", "role": "Coder", "task": "...", "assign_to_account_id": 2, "depends_on": ["t1"], "scope": ["src/api/"]}}
]}}
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
    scope: list[str] | None = None,
    peers: list[dict[str, Any]] | None = None,
    inbox: list[dict[str, Any]] | None = None,
) -> str:
    """One agent's subtask: role guidance + repository context + output contract."""
    guidance = ROLE_GUIDANCE.get(role, ROLE_GUIDANCE["Coder"])
    done_before = "\n".join(
        f"- task {t['id']} [{t['role']}]: {t.get('summary') or t['task'][:200]}" for t in (previous or [])
    ) or "(you are the first)"
    peer_lines = "\n".join(
        f"- {p['name']} [{p['role']}] area: {', '.join(p.get('scope') or []) or '(any)'} - {p['task'][:120]}"
        for p in (peers or [])
    ) or "(nobody)"
    inbox_lines = "\n".join(f"- from {m['from']}: {m['text']}" for m in (inbox or [])) or "(none)"
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

YOUR AREA - you may only write these paths: {", ".join(scope) if scope else "(anywhere in the repository)"}

Agents working in parallel with you (do not write their files):
{peer_lines}

Messages for you from the team:
{inbox_lines}

All files currently in the repository:
{_file_list(repo_files)}

Contents of the relevant files:
{context}

TEAM COMMUNICATION (optional, before the STATUS line):
MESSAGE to <role or agent name or all>: <one line - e.g. an interface you expose, a change others must follow>
QUESTION: <one line to the orchestrator if something is unclear; do your best meanwhile>

{OUTPUT_CONTRACT}
"""


def build_review_prompt(
    *,
    project_name: str,
    github_repo: str,
    project_task: str,
    round_no: int,
    max_rounds: int,
    completed: list[dict[str, Any]],
    repo_files: list[str] | None,
    questions: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    ci_text: str = "",
) -> str:
    """After a round: the orchestrator accepts the work or plans fixes."""
    work = "\n".join(
        f"- {t['key']} [{t['role']}] {t.get('agent', '')}: {t.get('summary') or '-'} (files: {', '.join(t.get('files') or []) or '-'})"
        for t in completed
    ) or "(nothing)"
    qs = "\n".join(f"- from {q['from']}: {q['text']}" for q in questions) or "(none)"
    account_lines = "\n".join(f"- account_id={a['id']} name={a['name']!r} role={a['default_role']}" for a in accounts)
    return f"""You are the ORCHESTRATOR of project "{project_name}" (repository {github_repo}). Review round {round_no} of {max_rounds}.

Project goal:
{project_task}

Work done so far:
{work}

Files in the repository now:
{_file_list(repo_files)}

Open questions from the team:
{qs}

{ci_text or "CI: not used for this project."}

Available accounts:
{account_lines}

Decide whether the project goal is met. Never answer "done" while CI is failing or missing (if CI is used):
give each CI error to the agent who owns the broken file. If parts are missing, wrong or do not fit together, plan FIX
subtasks (same format and rules as the plan: id, role, task, assign_to_account_id, depends_on, scope with
non-overlapping areas for parallel subtasks). Answer the open questions.

Reply with ONLY JSON:
{{"verdict": "done" | "continue",
  "answers": [{{"to": "<agent name or role>", "text": "..."}}],
  "tasks": [ ...fix subtasks, empty when done... ]}}
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
    ci_text: str = "",
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

{ci_text}

Write the final project summary for the user (mention the CI result if there is one):
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
