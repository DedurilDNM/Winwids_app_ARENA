"""Project orchestration: split -> per-task agents -> GitHub watcher -> summary.

`run_project_logic` is the transport-agnostic core (used by the arq worker
and by the inline fallback when Redis is unavailable). It is fully injectable
(adapter factory, watcher factory, browser manager, WS manager) so tests can
run it without a browser, Redis or GitHub.

Flow (per spec):
1. Orchestrator prompt -> JSON plan [{role, task, assign_to_account_id}].
2. Task rows created (pending), branch `task-<id>` per task.
3. Per task (sequential): agent prompt -> agent works in the repo via its own
   GitHub connector -> watcher polls /status/task-<id>.md for DONE/BLOCKED.
4. Final orchestrator prompt -> Project.result, status completed,
   WS `project_completed`.

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
from app.services.github_watcher import GitHubWatcher, build_watcher_for_user
from app.ws_manager import manager as default_ws_manager

logger = logging.getLogger(__name__)

AGENT_ROLES = ("Architect", "Coder", "Reviewer", "Tester", "Integrator")


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


async def _ask(adapter, context, *, chat_ref: str, prompt: str, timeout: int) -> str:
    """One adapter round-trip: open chat, send prompt, wait for the reply."""
    page = await adapter.open_chat(context, chat_ref)
    await adapter.send_message(page, prompt)
    # Belt-and-braces: never let a hung adapter hang the worker forever.
    return await asyncio.wait_for(adapter.wait_response(page, timeout=timeout), timeout=timeout + 60)


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
    watcher_factory=None,
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

    account_rows = [
        {"id": a.id, "name": a.name, "default_role": a.default_role}
        for a in assignment_accounts.values()
    ]
    split_prompt = prompts.build_split_prompt(
        project_name=project_snapshot["name"],
        description=project_snapshot["description"],
        task=project_snapshot["task"],
        github_repo=project_snapshot["github_repo"],
        accounts=account_rows,
    )
    try:
        raw_plan = await _ask(
            adapter, orchestrator_context,
            chat_ref=f"project-{project_id}",
            prompt=split_prompt,
            timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return await _fail_project(
            session_factory, project_id,
            f"Orchestrator did not respond: {type(exc).__name__}: {exc}",
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
            )
            session.add(task)
            await session.flush()
            task.branch = f"task-{task.id}"
            task_ids.append(task.id)
        await session.commit()
    for task_id in task_ids:
        await _set_task_status(session_factory, task_id, "pending", ws_manager=ws, project_id=project_id)

    watcher = watcher_factory() if watcher_factory else await build_watcher_for_user(
        session_factory, project_snapshot["user_id"]
    )

    # ---- execute tasks sequentially ---------------------------------------
    completed: list[dict[str, Any]] = []
    for task_id in task_ids:
        if not await _project_still_running(session_factory, project_id):
            logger.info("Project %s stopped by user; aborting task loop", project_id)
            return "failed"

        async with session_factory() as session:
            task = await session.get(Task, task_id)
            agent = await session.get(Account, task.assigned_account_id) if task.assigned_account_id else None
            task_role, task_text, branch = task.role, task.task, task.branch or f"task-{task_id}"

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

        agent_prompt = prompts.build_agent_prompt(
            project_name=project_snapshot["name"],
            github_repo=project_snapshot["github_repo"],
            role=task_role,
            task_text=task_text,
            task_id=task_id,
            branch=branch,
        )
        try:
            await _ask(
                adapter, agent_context,
                chat_ref=f"project-{project_id}-task-{task_id}",
                prompt=agent_prompt,
                timeout=settings.AGENT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            await _set_task_status(session_factory, task_id, "failed", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Agent for task {task_id} did not respond: {type(exc).__name__}: {exc}",
                ws_manager=ws,
            )

        outcome = await watcher.wait_for_task(
            repo=project_snapshot["github_repo"],
            task_id=task_id,
            branch=branch,
            timeout=task_timeout,
            poll_interval=poll_interval,
            should_continue=lambda: _project_still_running(session_factory, project_id),
        )
        if outcome == "done":
            await _set_task_status(session_factory, task_id, "done", ws_manager=ws, project_id=project_id)
            completed.append({"id": task_id, "role": task_role, "task": task_text, "branch": branch})
        elif outcome == "blocked":
            await _set_task_status(session_factory, task_id, "blocked", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Task {task_id} reported BLOCKED in status/task-{task_id}.md on branch {branch}.",
                ws_manager=ws,
            )
        else:
            await _set_task_status(session_factory, task_id, "failed", ws_manager=ws, project_id=project_id)
            return await _fail_project(
                session_factory, project_id,
                f"Task {task_id} timed out after {int(task_timeout)}s waiting for status/task-{task_id}.md (DONE/BLOCKED) on branch {branch}.",
                ws_manager=ws,
            )

    # ---- final orchestrator summary ---------------------------------------
    final_prompt = prompts.build_final_prompt(
        project_name=project_snapshot["name"],
        github_repo=project_snapshot["github_repo"],
        completed=completed,
    )
    try:
        summary = await _ask(
            adapter, orchestrator_context,
            chat_ref=f"project-{project_id}",
            prompt=final_prompt,
            timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return await _fail_project(
            session_factory, project_id,
            f"Orchestrator did not produce a final summary: {type(exc).__name__}: {exc}",
            ws_manager=ws,
        )

    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return "failed"
        project.status = "completed"
        project.result = summary
        project.completed_at = _utc_naive()
        await session.commit()

    await ws.send_to_project(
        project_id,
        _event(
            "project_completed", project_id, status="completed",
            message="Project completed",
            extra={"result": summary[:4000]},
        ),
    )
    logger.info("Project %s completed (%d tasks)", project_id, len(completed))
    return "completed"


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
