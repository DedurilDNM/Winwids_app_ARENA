"""Project orchestration: split -> per-task agents -> server commits -> summary + PR.

`run_project_logic` is the transport-agnostic core (used by the arq worker
and by the inline fallback when Redis is unavailable). It is fully injectable
(adapter factory, watcher factory, browser manager, WS manager) so tests can
run it without a browser, Redis or GitHub.

Flow:
0. GitHub: the user's token must be able to push; work branch
   `ai/project-<id>` is created from the default branch.
1. Orchestrator prompt (with the repo file list) -> JSON plan
   [{role, task, assign_to_account_id}].
2. Task rows created (pending).
3. Per task (sequential): agent prompt with role guidance, repo file list,
   contents of files from earlier tasks and the OUTPUT CONTRACT -> the reply
   is parsed into files (one "resend in format" retry) -> the SERVER commits
   them + status/task-<id>.md to the work branch. BLOCKED stops the project.
   Agents never need GitHub access (the service would require selecting the
   repository in every new chat).
4. Final orchestrator prompt -> pull request work branch -> default branch,
   Project.result/pr_url, status completed, WS `project_completed`.

Automation browser contexts are launched headless (no human watching);
manual-login browsers in the API process keep their display behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import or_, select

from app.adapters.base import load_adapter
from app.config import get_settings
from app.models import Account, Project, ProjectAssignment, Task
from app.services import prompts
from app.connectors.github import GitHubConnector, GitHubError
from app.services.agent_output import AgentOutputError, parse_agent_output
from app.services.github_watcher import load_github_token
from app.ws_manager import manager as default_ws_manager

logger = logging.getLogger(__name__)

AGENT_ROLES = ("Architect", "Coder", "Reviewer", "Tester", "Integrator", "ImagePrompter")
AGENT_FORMAT_ATTEMPTS = 2  # first reply + one "resend in the required format"
CONTEXT_BUDGET_BYTES = 60_000  # file contents shown to each agent


@dataclass
class PlanItem:
    role: str
    task: str
    assign_to_account_id: int


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _event(
    event_type: str,
    project_id: int,
    *,
    task_id: int | None = None,
    account_id: int | None = None,
    status: str | None = None,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "project_id": project_id,
        "task_id": task_id,
        "account_id": account_id,
        "status": status,
        "message": message,
        "ts": _utc_naive().isoformat(),
    }
    if extra:
        event.update(extra)
    return event


# ---------------------------------------------------------------- plan parsing

def parse_task_plan(
    raw: str,
    *,
    assignments: list[ProjectAssignment],
    orchestrator_account_id: int | None,
) -> list[PlanItem]:
    """Parse the orchestrator's JSON reply into validated plan items.

    Tolerates markdown fences and surrounding prose. Invalid items are
    skipped; an empty/invalid plan raises ValueError (caller fails the project).
    """
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Orchestrator reply does not contain a JSON array")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Orchestrator reply is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("Orchestrator reply contains an empty task plan")

    by_id = {a.account_id: a for a in assignments}
    plan: list[PlanItem] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        task_text = str(item.get("task") or "").strip()
        if role not in AGENT_ROLES or not task_text:
            continue
        account_id = _resolve_assignee(item.get("assign_to_account_id"), role, by_id, orchestrator_account_id)
        if account_id is None:
            continue
        plan.append(PlanItem(role=role, task=task_text, assign_to_account_id=account_id))
    if not plan:
        raise ValueError("Orchestrator plan has no valid tasks (bad role/task or unknown account)")
    return plan


def _resolve_assignee(
    raw_id: Any,
    role: str,
    by_id: dict[int, ProjectAssignment],
    orchestrator_account_id: int | None,
) -> int | None:
    try:
        candidate = int(raw_id)
    except (TypeError, ValueError):
        candidate = None
    if candidate is not None and candidate in by_id:
        return candidate
    # Fallbacks: same-role assignment -> orchestrator -> first assignment.
    for assignment in by_id.values():
        if assignment.role == role:
            return assignment.account_id
    if orchestrator_account_id is not None and orchestrator_account_id in by_id:
        return orchestrator_account_id
    if by_id:
        return next(iter(by_id))
    return orchestrator_account_id


# ------------------------------------------------------------------- helpers

async def _ensure_context(account: Account, *, session_factory, browser_manager):
    """Return a live browser context for the account, launching one if needed.

    In the API process this reuses the VNC browser; in the worker process it
    launches a fresh headless browser from the encrypted storage_state.
    Returns None when the account has no saved session or launch fails.
    """
    context = browser_manager.get_context(account.id)
    if context is not None:
        return context
    if not account.storage_state_encrypted:
        return None
    from app.security import decrypt_bytes

    try:
        storage_state = json.loads(decrypt_bytes(account.storage_state_encrypted))
    except Exception:
        logger.warning("Could not decrypt storage_state for account %s", account.id)
        return None
    try:
        return await browser_manager.launch(
            account.id,
            storage_state=storage_state,
            headless=True,
            open_url=False,
        )
    except Exception:
        logger.warning("Could not launch browser for account %s", account.id, exc_info=True)
        return None


async def _retryable_sleep(attempt: int, settings) -> None:
    """Exponential backoff before a retry: backoff * 2^(attempt-1)."""
    delay = settings.ADAPTER_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
    logger.warning("Adapter transient error, retrying in %.1fs (attempt %s)", delay, attempt)
    await asyncio.sleep(delay)


def _is_retryable(exc: BaseException) -> bool:
    """Errors that are safe to retry (open_chat / send_message failures only)."""
    from app.services.browser_manager import BrowserUnavailableError

    return isinstance(
        exc,
        (BrowserUnavailableError, OSError, TimeoutError),
    )


async def _ask(adapter, context, *, chat_ref: str, prompt: str, timeout: int, notify=None, should_continue=None) -> str:
    """One adapter round-trip: open chat, send prompt, wait for the reply.

    open_chat + send_message are retried with exponential backoff
    (ADAPTER_MAX_RETRIES) on transient errors; wait_response is NOT retried
    to avoid sending the same prompt twice if the service already received it.
    """
    settings = get_settings()
    last_exc: BaseException | None = None

    if hasattr(adapter, "on_attention"):
        adapter.on_attention = notify
    pausable = hasattr(adapter, "should_continue")
    if pausable:
        adapter.should_continue = should_continue
    for attempt in range(1, settings.ADAPTER_MAX_RETRIES + 1):
        page = None
        try:
            page = await adapter.open_chat(context, chat_ref)
            await adapter.send_message(page, prompt)
            if pausable:
                # The adapter enforces its own deadline, extended while a task is
                # paused for a human (captcha / pop-up / login) - no outer cap.
                reply = await adapter.wait_response(page, timeout=timeout)
            else:
                # Belt-and-braces: never let a hung adapter hang the worker forever.
                reply = await asyncio.wait_for(
                    adapter.wait_response(page, timeout=timeout), timeout=timeout + 60
                )
            if hasattr(adapter, "after_reply"):
                await adapter.after_reply(page)
            return reply
        except TimeoutError as exc:
            # wait_response timeout: message may already be with the service -
            # retrying could duplicate it, so fail fast.
            raise
        except RuntimeError as exc:
            # login_required / service error: retrying won't help without
            # re-login, so fail fast.
            msg = str(exc)
            if any(k in msg.lower() for k in ("login", "captcha", "pop-up", "stopped by the user")):
                raise  # retrying cannot help and could duplicate the message
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 - transient browser/network error
            last_exc = exc
        finally:
            # One tab per message: close it, the conversation stays on the service.
            close = getattr(page, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass

        if attempt < settings.ADAPTER_MAX_RETRIES:
            await _retryable_sleep(attempt, settings)

    raise RuntimeError(
        f"Adapter failed after {settings.ADAPTER_MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def _notifier(ws, *, user_id: int, project_id: int, account, session_factory=None, task_id: int | None = None):
    """Adapter callback: a captcha / pop-up / logout needs the user ->
    attention list + WS event; the task shows as «paused» meanwhile."""
    from app.services.attention import raise_attention, resolve_attention

    async def notify(kind: str, active: bool) -> None:
        if task_id is not None and session_factory is not None:
            await _set_task_status(session_factory, task_id, "paused" if active else "running",
                                   ws_manager=ws, project_id=project_id)
        if active:
            item = raise_attention(user_id, account_id=account.id, account_name=account.name,
                                   project_id=project_id, kind=kind)
            await ws.send_to_project(project_id, _event(
                "attention_needed", project_id, account_id=account.id, status=kind,
                message=f"{account.name}: {item['message']}", extra={"kind": kind, "account_name": account.name},
            ))
        else:
            resolve_attention(user_id, account_id=account.id, kind=kind)
            await ws.send_to_project(project_id, _event(
                "attention_resolved", project_id, account_id=account.id, status=kind,
                message=f"{account.name}: resolved", extra={"kind": kind},
            ))

    return notify


def _explain(exc: BaseException, who: str) -> str:
    """User-facing reason for an adapter failure."""
    msg = str(exc)
    if "captcha" in msg.lower():
        return (f"{who}: the service is asking for a captcha. Open «Смотреть агента», "
                "solve it in the browser and start the project again.")
    if "pop-up" in msg.lower():
        return (f"{who}: a pop-up window in the service blocks sending. Open «Смотреть агента», "
                "close it and start the project again.")
    if "login" in msg.lower():
        return f"{who}: the account is logged out. Log in again (Аккаунты → «Перелогиниться») and restart."
    return f"{who} did not respond: {type(exc).__name__}: {msg}"


async def _fail_project(session_factory, project_id: int, reason: str, *, ws_manager) -> str:
    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return "failed"
        project.status = "failed"
        project.result = reason
        project.completed_at = _utc_naive()
        await session.commit()
    await ws_manager.send_to_project(
        project_id,
        _event("project_failed", project_id, status="failed", message=reason[:500]),
    )
    logger.info("Project %s failed: %s", project_id, reason[:200])
    return "failed"


async def _set_task_status(session_factory, task_id: int, status: str, *, ws_manager, project_id: int) -> None:
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        task.status = status
        await session.commit()
    await ws_manager.send_to_project(
        project_id,
        _event(
            "task_status_changed",
            project_id,
            task_id=task_id,
            account_id=task.assigned_account_id,
            status=status,
            message=f"Task {task_id} -> {status}",
        ),
    )


async def _project_still_running(session_factory, project_id: int) -> bool:
    async with session_factory() as session:
        project = await session.get(Project, project_id)
        return project is not None and project.status == "running"


# ------------------------------------------------------------------ main flow

async def run_project_logic(
    project_id: int,
    *,
    session_factory,
    adapter_factory: Callable[[], Any] | None = None,
    github_factory: Callable[[str], Any] | None = None,
    watcher_factory=None,  # deprecated, unused: the server commits itself now
    browser_manager,
    ws_manager=None,
    poll_interval: float | None = None,
    task_timeout: float | None = None,
) -> str:
    """Run one project end-to-end. Returns the final project status."""
    settings = get_settings()
    ws = ws_manager or default_ws_manager
    poll_interval = settings.GITHUB_POLL_INTERVAL_SECONDS if poll_interval is None else poll_interval
    task_timeout = settings.TASK_TIMEOUT_SECONDS if task_timeout is None else task_timeout
    adapter = (adapter_factory or load_adapter)()

    # ---- load project + people -------------------------------------------
    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            logger.warning("run_project: project %s not found", project_id)
            return "failed"
        if project.status != "running":
            logger.info("run_project: project %s is %s, not running - aborting", project_id, project.status)
            return project.status
        project_snapshot = {
            "id": project.id,
            "user_id": project.user_id,
            "name": project.name,
            "description": project.description,
            "task": project.task,
            "github_repo": project.github_repo,
            "orchestrator_account_id": project.orchestrator_account_id,
        }
        assignments = (
            await session.scalars(
                select(ProjectAssignment).where(ProjectAssignment.project_id == project_id)
            )
        ).all()
        orchestrator = (
            await session.get(Account, project.orchestrator_account_id)
            if project.orchestrator_account_id
            else None
        )
        assignment_accounts = {}
        for assignment in assignments:
            account = await session.get(Account, assignment.account_id)
            if account is not None:
                assignment_accounts[assignment.account_id] = account

    if orchestrator is None:
        return await _fail_project(
            session_factory, project_id, "Project has no orchestrator account.", ws_manager=ws
        )

    await ws.send_to_project(
        project_id,
        _event("project_started", project_id, status="running", message="Project started"),
    )

    # ---- GitHub: token, permissions, project work branch ------------------
    repo = project_snapshot["github_repo"]
    token = await load_github_token(session_factory, project_snapshot["user_id"])
    if not token:
        return await _fail_project(
            session_factory, project_id,
            "GitHub is not connected. Open the «GitHub» tab, add a token with write access to the repository and restart the project.",
            ws_manager=ws,
        )
    gh = (github_factory or _default_github)(token)
    work_branch = f"ai/project-{project_id}"
    try:
        repo_meta = await gh.get_repo(repo)
        if not (repo_meta.get("permissions") or {}).get("push", False):
            return await _fail_project(
                session_factory, project_id,
                f"The GitHub token cannot write to {repo}. Give it 'Contents: Read and write' and 'Pull requests: Read and write'.",
                ws_manager=ws,
            )
        base_branch = repo_meta.get("default_branch") or "main"
        await gh.ensure_branch(repo, work_branch, base_branch)
        repo_files = await gh.list_tree(repo, work_branch)
    except GitHubError as exc:
        return await _fail_project(session_factory, project_id, f"GitHub: {exc.message}", ws_manager=ws)

    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is not None:
            project.work_branch = work_branch
            await session.commit()

    # ---- orchestrator: split the task ------------------------------------
    orchestrator_context = await _ensure_context(orchestrator, session_factory=session_factory, browser_manager=browser_manager)
    if orchestrator_context is None:
        await ws.send_to_project(
            project_id,
            _event(
                "session_lost", project_id, account_id=orchestrator.id,
                message="Orchestrator browser session is not available",
            ),
        )
        return await _fail_project(
            session_factory, project_id,
            "Orchestrator account has no live browser session. Log in again via VNC and restart the project.",
            ws_manager=ws,
        )

    # The role chosen for this project wins over the account's default role.
    project_roles = {a.account_id: a.role for a in assignments}
    account_rows = [
        {"id": a.id, "name": a.name, "default_role": project_roles.get(a.id, a.default_role)}
        for a in assignment_accounts.values()
    ]
    split_prompt = prompts.build_split_prompt(
        project_name=project_snapshot["name"],
        description=project_snapshot["description"],
        task=project_snapshot["task"],
        github_repo=repo,
        accounts=account_rows,
        repo_files=repo_files,
    )
    try:
        raw_plan = await _ask(
            adapter, orchestrator_context,
            chat_ref=f"project-{project_id}",
            prompt=split_prompt,
            timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
            notify=_notifier(ws, user_id=project_snapshot["user_id"], project_id=project_id, account=orchestrator),
            should_continue=lambda: _project_still_running(session_factory, project_id),
        )
    except Exception as exc:
        return await _fail_project(
            session_factory, project_id,
            _explain(exc, "Orchestrator"),
            ws_manager=ws,
        )

    try:
        plan = parse_task_plan(
            raw_plan,
            assignments=list(assignments),
            orchestrator_account_id=project_snapshot["orchestrator_account_id"],
        )
    except ValueError as exc:
        return await _fail_project(
            session_factory, project_id,
            f"Could not parse orchestrator plan: {exc}. Raw reply (truncated): {raw_plan[:500]}",
            ws_manager=ws,
        )
    for item in plan:
        if item.assign_to_account_id in project_roles and project_roles[item.assign_to_account_id] in AGENT_ROLES:
            item.role = project_roles[item.assign_to_account_id]
    logger.info("Project %s: orchestrator produced %d subtasks", project_id, len(plan))

    # ---- create Task rows -------------------------------------------------
    task_ids: list[int] = []
    async with session_factory() as session:
        for item in plan:
            task = Task(
                project_id=project_id,
                role=item.role,
                task=item.task,
                assigned_account_id=item.assign_to_account_id,
                status="pending",
                branch=work_branch,
            )
            session.add(task)
            await session.flush()
            task_ids.append(task.id)
        await session.commit()
    for task_id in task_ids:
        await _set_task_status(session_factory, task_id, "pending", ws_manager=ws, project_id=project_id)

    # ---- execute tasks sequentially (each sees the previous tasks' files) --
    completed: list[dict[str, Any]] = []
    touched: list[str] = []  # files written by earlier tasks, newest last
    for task_id in task_ids:
        if not await _project_still_running(session_factory, project_id):
            logger.info("Project %s stopped by user; aborting task loop", project_id)
            return "failed"

        async with session_factory() as session:
            task = await session.get(Task, task_id)
            agent = await session.get(Account, task.assigned_account_id) if task.assigned_account_id else None
            task_role, task_text = task.role, task.task

        if agent is None:
            await _set_task_status(session_factory, task_id, "blocked", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Task {task_id} has no assigned account (it may have been deleted).",
                ws_manager=ws,
            )

        agent_context = await _ensure_context(agent, session_factory=session_factory, browser_manager=browser_manager)
        if agent_context is None:
            await ws.send_to_project(
                project_id,
                _event(
                    "session_lost", project_id, task_id=task_id, account_id=agent.id,
                    message=f"Agent account {agent.id} has no live browser session",
                ),
            )
            await _set_task_status(session_factory, task_id, "blocked", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Agent account {agent.id} ({agent.name}) has no live browser session. Log in again via VNC.",
                ws_manager=ws,
            )

        await _set_task_status(session_factory, task_id, "running", ws_manager=ws, project_id=project_id)
        await ws.send_to_project(
            project_id,
            _event(
                "task_started", project_id, task_id=task_id, account_id=agent.id,
                status="running", message=f"Task {task_id} started ({task_role})",
            ),
        )

        try:
            repo_files = await gh.list_tree(repo, work_branch)
            context_files = await _collect_context(gh, repo, work_branch, touched, repo_files)
        except GitHubError as exc:
            await _set_task_status(session_factory, task_id, "failed", ws_manager=ws, project_id=project_id)
            return await _fail_project(session_factory, project_id, f"GitHub: {exc.message}", ws_manager=ws)

        agent_prompt = prompts.build_agent_prompt(
            project_name=project_snapshot["name"],
            github_repo=repo,
            role=task_role,
            task_text=task_text,
            task_id=task_id,
            branch=work_branch,
            project_task=project_snapshot["task"],
            repo_files=repo_files,
            context_files=context_files,
            previous=completed,
        )
        output = None
        prompt = agent_prompt
        last_error = ""
        for attempt in range(1, AGENT_FORMAT_ATTEMPTS + 1):
            try:
                reply = await _ask(
                    adapter, agent_context,
                    chat_ref=f"project-{project_id}-task-{task_id}",
                    prompt=prompt,
                    timeout=settings.AGENT_TIMEOUT_SECONDS,
                    notify=_notifier(ws, user_id=project_snapshot["user_id"], project_id=project_id, account=agent,
                                     session_factory=session_factory, task_id=task_id),
                    should_continue=lambda: _project_still_running(session_factory, project_id),
                )
            except Exception as exc:
                await _set_task_status(session_factory, task_id, "failed", ws_manager=ws, project_id=project_id)
                return await _fail_project(
                    session_factory, project_id,
                    _explain(exc, f"Agent for task {task_id} ({agent.name})"),
                    ws_manager=ws,
                )
            try:
                output = parse_agent_output(reply)
                break
            except AgentOutputError as exc:
                last_error = str(exc)
                logger.info("Task %s: reply rejected (attempt %d): %s", task_id, attempt, last_error)
                prompt = prompts.build_fix_prompt(last_error) + "\n\nTHE ORIGINAL TASK:\n\n" + agent_prompt

        if output is None:
            await _set_task_status(session_factory, task_id, "failed", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Task {task_id}: the agent's reply did not follow the required format ({last_error}).",
                ws_manager=ws,
            )

        if output.status == "BLOCKED":
            await _save_task_result(session_factory, task_id, summary=output.summary, commit_sha=None)
            await _set_task_status(session_factory, task_id, "blocked", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Task {task_id} ({task_role}) is BLOCKED: {output.summary}",
                ws_manager=ws,
            )

        files = dict(output.files)
        files[f"status/task-{task_id}.md"] = f"DONE\n\n[{task_role}] {output.summary}\n"
        first_line = (output.summary.splitlines() or [""])[0][:72]
        try:
            commit_sha = await gh.commit_files(
                repo, work_branch, files,
                message=f"task-{task_id} [{task_role}]: {first_line or task_text[:72]}",
            )
        except GitHubError as exc:
            await _set_task_status(session_factory, task_id, "failed", ws_manager=ws, project_id=project_id)
            return await _fail_project(session_factory, project_id, f"GitHub commit failed: {exc.message}", ws_manager=ws)

        await _save_task_result(session_factory, task_id, summary=output.summary, commit_sha=commit_sha)
        await _set_task_status(session_factory, task_id, "done", ws_manager=ws, project_id=project_id)
        for path in output.files:
            if path in touched:
                touched.remove(path)
            touched.append(path)
        completed.append({
            "id": task_id, "role": task_role, "task": task_text, "branch": work_branch,
            "summary": output.summary, "commit": commit_sha, "files": sorted(output.files),
        })

    # ---- final orchestrator summary + pull request -------------------------
    final_prompt = prompts.build_final_prompt(
        project_name=project_snapshot["name"],
        github_repo=repo,
        completed=completed,
        branch=work_branch,
    )
    try:
        summary = await _ask(
            adapter, orchestrator_context,
            chat_ref=f"project-{project_id}",
            prompt=final_prompt,
            timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
            notify=_notifier(ws, user_id=project_snapshot["user_id"], project_id=project_id, account=orchestrator),
            should_continue=lambda: _project_still_running(session_factory, project_id),
        )
    except Exception as exc:
        return await _fail_project(
            session_factory, project_id,
            f"Orchestrator did not produce a final summary: {type(exc).__name__}: {exc}",
            ws_manager=ws,
        )

    pr_url: str | None = None
    try:
        pr_url = await gh.create_pull_request(
            repo, head=work_branch, base=base_branch,
            title=f"[AI] {project_snapshot['name']}",
            body=f"{summary}\n\n---\nCommits made by AI Dev Department on behalf of the project agents.",
        )
    except GitHubError as exc:
        logger.warning("Project %s: pull request not created: %s", project_id, exc.message)
        summary += f"\n\n(Pull request was not created: {exc.message}. The work is on branch {work_branch}.)"

    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return "failed"
        project.status = "completed"
        project.result = summary
        project.pr_url = pr_url
        project.completed_at = _utc_naive()
        await session.commit()

    await ws.send_to_project(
        project_id,
        _event(
            "project_completed", project_id, status="completed",
            message="Project completed",
            extra={"result": summary[:4000], "pr_url": pr_url},
        ),
    )
    logger.info("Project %s completed (%d tasks, PR %s)", project_id, len(completed), pr_url)
    return "completed"


async def _save_task_result(session_factory, task_id: int, *, summary: str, commit_sha: str | None) -> None:
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is not None:
            task.summary = summary[:4000]
            task.commit_sha = commit_sha
            await session.commit()


async def _collect_context(gh, repo: str, branch: str, touched: list[str], repo_files: list[str]) -> dict[str, str]:
    """File contents to show the next agent: files written by earlier tasks
    (newest first), then small top-level docs, within CONTEXT_BUDGET_BYTES."""
    wanted = list(reversed(touched))
    for name in ("README.md", "readme.md", "README"):
        if name in repo_files and name not in wanted:
            wanted.append(name)
    out: dict[str, str] = {}
    used = 0
    for path in wanted:
        content = await gh.read_file(repo, path, ref=branch)
        if content is None:
            continue
        size = len(content.encode())
        if used + size > CONTEXT_BUDGET_BYTES:
            continue
        out[path] = content
        used += size
    return out


def _default_github(token: str):
    return GitHubConnector(token=token)


# ------------------------------------------------------------ session health

async def _affected_project_ids(session_factory, account_id: int) -> list[int]:
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Project.id).where(
                    or_(
                        Project.orchestrator_account_id == account_id,
                        Project.id.in_(
                            select(ProjectAssignment.project_id).where(
                                ProjectAssignment.account_id == account_id
                            )
                        ),
                    )
                )
            )
        ).all()
    return list(rows)


async def check_all_sessions(
    *,
    session_factory,
    adapter_factory: Callable[[], Any] | None = None,
    browser_manager,
    ws_manager=None,
) -> dict[str, int]:
    """Cron body (every 5 min): is_alive per active account.

    active + dead  -> relogin_needed + WS session_lost to affected projects
    relogin_needed + alive again -> active + WS session_relogged
    """
    ws = ws_manager or default_ws_manager
    adapter = (adapter_factory or load_adapter)()
    counts = {"checked": 0, "lost": 0, "recovered": 0}

    async with session_factory() as session:
        accounts = (
            await session.scalars(
                select(Account).where(Account.status.in_(("active", "relogin_needed")))
            )
        ).all()
        account_ids = [a.id for a in accounts]

    for account_id in account_ids:
        async with session_factory() as session:
            account = await session.get(Account, account_id)
            if account is None or account.status not in ("active", "relogin_needed"):
                continue
            previous_status = account.status
            context = await _ensure_context(account, session_factory=session_factory, browser_manager=browser_manager)
            alive = False
            if context is not None:
                try:
                    alive = bool(await adapter.is_alive(context))
                except Exception:
                    logger.warning("is_alive failed for account %s", account_id, exc_info=True)
            account.last_health_check = _utc_naive()
            counts["checked"] += 1
            if alive and previous_status == "relogin_needed":
                account.status = "active"
                counts["recovered"] += 1
            elif not alive and previous_status == "active":
                account.status = "relogin_needed"
                counts["lost"] += 1
            await session.commit()

        if counts and previous_status != account.status:
            event_type = "session_relogged" if account.status == "active" else "session_lost"
            for pid in await _affected_project_ids(session_factory, account_id):
                await ws.send_to_project(
                    pid,
                    _event(
                        event_type, pid, account_id=account_id,
                        status=account.status,
                        message=f"Account {account_id} session {'recovered' if account.status == 'active' else 'lost'}",
                    ),
                )
    if counts["checked"]:
        logger.info("Session check: %s", counts)
    return counts
