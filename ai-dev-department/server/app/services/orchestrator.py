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
from app.services.agent_output import AgentOutputError, _norm_scope, check_scope, in_scope, parse_agent_output
from app.services.github_watcher import load_github_token
from app.ws_manager import manager as default_ws_manager

logger = logging.getLogger(__name__)

AGENT_ROLES = ("Architect", "Coder", "Reviewer", "Tester", "Integrator", "ImagePrompter")
TASK_RETRIES = 2  # automatic retries of a task that failed for a temporary reason
PLAN_FORMAT_ATTEMPTS = 3  # orchestrator plan: first reply + two "resend valid JSON"
TASK_RETRIES = 2  # automatic retries of a task that failed for a temporary reason
PLAN_FORMAT_ATTEMPTS = 3  # orchestrator plan: first reply + two "resend valid JSON"
AGENT_FORMAT_ATTEMPTS = 3  # first reply + two "resend complete / in the required format"
CONTEXT_BUDGET_BYTES = 60_000  # file contents shown to each agent


@dataclass
class PlanItem:
    role: str
    task: str
    assign_to_account_id: int
    key: str = ""
    depends_on: list = None  # keys of plan items this one waits for
    scope: list = None       # paths this task may write (its area)

    def __post_init__(self) -> None:
        self.depends_on = list(self.depends_on or [])
        self.scope = list(self.scope or [])


@dataclass
class Review:
    done: bool
    answers: list  # [(to, text)]
    tasks: list    # [PlanItem]


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
    data = _extract_json(raw)
    if isinstance(data, dict):
        data = data.get("tasks")
    if not isinstance(data, list) or not data:
        raise ValueError("Orchestrator reply contains an empty task plan")
    return _plan_items(data, assignments=assignments, orchestrator_account_id=orchestrator_account_id)


def _extract_json(raw: str):
    """The first JSON object/array in a reply (tolerates fences and prose)."""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        raise ValueError("Orchestrator reply does not contain JSON")
    start = min(starts)
    end = text.rfind("]" if text[start] == "[" else "}")
    if end < start:
        raise ValueError("Orchestrator reply does not contain complete JSON")
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate, strict=False)  # strict=False: raw newlines inside strings
    except json.JSONDecodeError as exc:
        try:
            return json.loads(_repair_json(candidate), strict=False)
        except json.JSONDecodeError:
            raise ValueError(f"Orchestrator reply is not valid JSON: {exc}") from exc


def _repair_json(text: str) -> str:
    """Escape double quotes that sit INSIDE string values.

    Chat services render replies as markdown, which turns the escape \\" into a
    bare ", so `"task": "make the \\"seeds\\" layer"` arrives as
    `"task": "make the "seeds" layer"`. A quote closes a string only when the
    next non-space character is , : } ] (or the end); otherwise it is escaped.
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == '"':
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                closes = j >= n or text[j] in ":}]"
                if not closes and text[j] == ",":
                    k = j + 1
                    while k < n and text[k] in " \t\r\n":
                        k += 1
                    # after a real comma comes the next JSON value or key
                    closes = k >= n or text[k] in '"{[-0123456789' or text.startswith(("true", "false", "null"), k)
                if closes:
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
            else:
                out.append(c)
        else:
            if c == '"':
                in_str = True
            out.append(c)
        i += 1
    return "".join(out)


def _plan_items(data: list, *, assignments, orchestrator_account_id, key_prefix: str = "t") -> list[PlanItem]:
    by_id = {a.account_id: a for a in assignments}
    plan: list[PlanItem] = []
    for n, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        task_text = str(item.get("task") or "").strip()
        if role not in AGENT_ROLES or not task_text:
            continue
        account_id = _resolve_assignee(item.get("assign_to_account_id"), role, by_id, orchestrator_account_id)
        if account_id is None:
            continue
        key = str(item.get("id") or f"{key_prefix}{n}").strip()[:32]
        deps = item.get("depends_on") or []
        scope = item.get("scope") or item.get("files") or []
        plan.append(PlanItem(
            role=role, task=task_text, assign_to_account_id=account_id, key=key,
            depends_on=[str(d).strip() for d in deps if str(d).strip()] if isinstance(deps, list) else [],
            scope=[_norm_scope(str(x)) for x in scope if str(x).strip()][:20] if isinstance(scope, list) else [],
        ))
    if not plan:
        raise ValueError("Orchestrator plan has no valid tasks (bad role/task or unknown account)")
    keys = {p.key for p in plan}
    if len(keys) != len(plan):
        raise ValueError("Orchestrator plan has duplicate task ids")
    for p in plan:
        p.depends_on = [d for d in p.depends_on if d in keys and d != p.key]
    _check_acyclic(plan)
    return plan


def _check_acyclic(plan: list[PlanItem]) -> None:
    deps = {p.key: set(p.depends_on) for p in plan}
    done: set[str] = set()
    while deps:
        ready = [k for k, d in deps.items() if d <= done]
        if not ready:
            raise ValueError(f"Orchestrator plan has circular dependencies between {sorted(deps)}")
        for k in ready:
            done.add(k)
            del deps[k]


def parse_review(raw: str, *, assignments, orchestrator_account_id, round_no: int) -> Review:
    """Orchestrator's verdict after a round: {"verdict": "done"|"continue",
    "answers": [{"to", "text"}], "tasks": [plan items]}."""
    data = _extract_json(raw)
    if isinstance(data, list):  # a bare task list means "continue with these"
        data = {"verdict": "continue", "tasks": data}
    if not isinstance(data, dict):
        raise ValueError("Review reply is not a JSON object")
    answers = [
        (str(a.get("to") or "all")[:120], str(a.get("text") or "").strip())
        for a in (data.get("answers") or []) if isinstance(a, dict) and str(a.get("text") or "").strip()
    ]
    verdict = str(data.get("verdict") or "").lower()
    raw_tasks = data.get("tasks") or []
    if verdict == "done" or not raw_tasks:
        return Review(done=True, answers=answers, tasks=[])
    tasks = _plan_items(raw_tasks, assignments=assignments, orchestrator_account_id=orchestrator_account_id,
                        key_prefix=f"r{round_no}t")
    return Review(done=False, answers=answers, tasks=tasks)


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
        if browser_manager is _real_browser_manager():
            from app.services.vnc_manager import vnc_manager

            # Own display: the user can watch / help (captcha) without disturbing the agent.
            return await vnc_manager.launch_on_own_display(account.id, storage_state)
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


_PERMANENT = ("is BLOCKED", "Stopped by", "Workflows", "no assigned account", "captcha", "pop-up",
              "logged out", "Log in again", "no live browser session", "cannot write")


def _retryable(reason: str) -> bool:
    """Temporary failures (no reply, browser/network/GitHub hiccup, unusable
    reply) are retried; a human or a permission is needed for the rest."""
    return not any(marker.lower() in reason.lower() for marker in _PERMANENT)


async def _load_resume(session_factory, project_id: int, accounts: dict) -> dict | None:
    """State to continue a stopped project: finished tasks (as context) and the
    unfinished tasks of the latest round (reset to pending)."""
    async with session_factory() as session:
        tasks = (await session.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id))).all()
        if not tasks:
            return None
        round_no = max(t.round or 0 for t in tasks)
        done = [t for t in tasks if t.status == "done"]
        todo = [t for t in tasks if (t.round or 0) == round_no and t.status != "done"]
        for t in todo:
            t.status, t.attempts = "pending", 0
        await session.commit()
        completed = [{
            "id": t.id, "key": t.plan_key or f"t{t.id}", "role": t.role, "task": t.task, "branch": t.branch,
            "summary": t.summary or "", "commit": t.commit_sha, "files": list(t.files or []),
            "account_id": t.assigned_account_id,
            "agent": getattr(accounts.get(t.assigned_account_id), "name", "?"),
        } for t in done]
        specs = {t.id: {"id": t.id, "key": t.plan_key or f"t{t.id}", "role": t.role, "task": t.task,
                        "account_id": t.assigned_account_id, "scope": list(t.scope or []),
                        "deps": set(t.depends_on or []), "attempts": 0} for t in todo}
        touched = [f for t in done for f in (t.files or [])]
        return {"round": round_no, "completed": completed, "specs": specs,
                "done_ids": {t.id for t in done}, "touched": touched}


def _real_browser_manager():
    from app.services.browser_manager import browser_manager

    return browser_manager


class _Chat:
    """One conversation in the service. The first ask() opens a new chat (with
    retries on transient errors); later asks - e.g. «resend in the required
    format» - go to the SAME chat, so the agent keeps its context. close()
    closes the tab (the conversation stays on the service)."""

    def __init__(self, adapter, context, *, chat_ref: str, timeout: int, notify=None, should_continue=None,
                 refresh_context=None) -> None:
        self.adapter, self.context, self.chat_ref, self.timeout = adapter, context, chat_ref, timeout
        self.refresh_context = refresh_context  # async () -> context: the CURRENT browser of the account
        self.page = None
        if hasattr(adapter, "on_attention"):
            adapter.on_attention = notify
        self.pausable = hasattr(adapter, "should_continue")
        if self.pausable:
            adapter.should_continue = should_continue

    async def _wait(self, page) -> str:
        if self.pausable:
            # The adapter enforces its own deadline, extended while a task is
            # paused for a human (captcha / pop-up / login) - no outer cap.
            reply = await self.adapter.wait_response(page, timeout=self.timeout)
        else:
            # Belt-and-braces: never let a hung adapter hang the worker forever.
            reply = await asyncio.wait_for(self.adapter.wait_response(page, timeout=self.timeout),
                                           timeout=self.timeout + 60)
        if hasattr(self.adapter, "after_reply"):
            await self.adapter.after_reply(page)
        return reply

    async def ask(self, prompt: str) -> str:
        if self.page is not None:  # follow-up in the same chat
            await self.adapter.send_message(self.page, prompt)
            return await self._wait(self.page)

        settings = get_settings()
        last_exc: BaseException | None = None
        for attempt in range(1, settings.ADAPTER_MAX_RETRIES + 1):
            page = None
            try:
                page = await self.adapter.open_chat(self.context, self.chat_ref)
                await self.adapter.send_message(page, prompt)
                reply = await self._wait(page)
                self.page, page = page, None  # keep the chat open for follow-ups
                return reply
            except TimeoutError:
                # wait_response timeout: the message may already be with the
                # service - retrying could duplicate it, so fail fast.
                raise
            except RuntimeError as exc:
                msg = str(exc)
                if any(k in msg.lower() for k in ("login", "captcha", "pop-up", "stopped by the user")):
                    raise  # retrying cannot help and could duplicate the message
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 - transient browser/network error
                last_exc = exc
            finally:
                await _close_page(page)
            if attempt < settings.ADAPTER_MAX_RETRIES:
                await _retryable_sleep(attempt, settings)
                if self.refresh_context is not None:  # the browser may have been restarted meanwhile
                    try:
                        self.context = await self.refresh_context() or self.context
                    except Exception:
                        logger.warning("Could not refresh the browser context", exc_info=True)
        raise RuntimeError(f"Adapter failed after {settings.ADAPTER_MAX_RETRIES} attempts: {last_exc}") from last_exc

    async def close(self) -> None:
        await _close_page(self.page)
        self.page = None


async def _close_page(page) -> None:
    close = getattr(page, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:
            pass


async def _ask(adapter, context, *, chat_ref: str, prompt: str, timeout: int, notify=None, should_continue=None) -> str:
    """One-shot question in a new chat (the tab is closed afterwards)."""
    chat = _Chat(adapter, context, chat_ref=chat_ref, timeout=timeout, notify=notify, should_continue=should_continue)
    try:
        return await chat.ask(prompt)
    finally:
        await chat.close()


def _save_reply(project_id: int, name: str, text: str) -> None:
    """Keep an unparsable reply on the server for troubleshooting (never sent anywhere)."""
    try:
        from app.config import DATA_DIR

        folder = DATA_DIR / "replies"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"project-{project_id}-{name}.txt").write_text(text or "", encoding="utf-8")
    except OSError:
        logger.warning("Could not save the raw reply", exc_info=True)


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


# ------------------------------------------------------------ parallel rounds

@dataclass
class _Run:
    """Executes plans for one project run: dependency graph, parallel agents
    (one task per account at a time), area checks, serialized commits,
    messages between agents."""

    project_id: int
    snapshot: dict
    repo: str
    branch: str
    gh: Any
    ws: Any
    session_factory: Any
    browser_manager: Any
    make_adapter: Any
    settings: Any
    accounts: dict
    orchestrator: Any
    completed: list = None
    touched: list = None

    def __post_init__(self) -> None:
        self.completed = []
        self.touched = []
        self._commit_lock = asyncio.Lock()
        self._running: dict[int, dict] = {}  # task_id -> info (for "peers")

    # ---- messages ------------------------------------------------------
    async def post_message(self, *, from_task_id, from_account, to: str, text: str, kind: str = "message") -> None:
        from app.models import AgentMessage

        name = getattr(from_account, "name", "orchestrator") if from_account is not None else "orchestrator"
        async with self.session_factory() as session:
            session.add(AgentMessage(
                project_id=self.project_id, from_task_id=from_task_id,
                from_account_id=getattr(from_account, "id", None), from_name=name,
                to=to[:120], kind=kind, text=text[:2000], delivered_to=[],
            ))
            await session.commit()
        await self.ws.send_to_project(self.project_id, _event(
            "agent_message", self.project_id, task_id=from_task_id, account_id=getattr(from_account, "id", None),
            status=kind, message=f"{name} → {to}: {text[:300]}", extra={"from": name, "to": to, "text": text[:2000], "kind": kind},
        ))

    async def inbox_for(self, account) -> list[dict]:
        """Undelivered messages for this agent (by name, role or "all"); marks them delivered."""
        from app.models import AgentMessage

        names = {account.name.lower(), "all", "everyone", "team"}
        roles = {r.lower() for r in self._roles_of(account.id)}
        out = []
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(AgentMessage).where(AgentMessage.project_id == self.project_id,
                                           AgentMessage.kind.in_(("message", "answer")))
                .order_by(AgentMessage.id)
            )).all()
            for m in rows:
                to = m.to.strip().lower().rstrip("s")
                if m.from_account_id == account.id or account.id in (m.delivered_to or []):
                    continue
                if to in names or m.to.strip().lower() in names or to in {r.rstrip("s") for r in roles}:
                    out.append({"from": m.from_name, "text": m.text})
                    m.delivered_to = [*(m.delivered_to or []), account.id]
            await session.commit()
        return out[-15:]

    async def take_questions(self) -> list[dict]:
        from app.models import AgentMessage

        out = []
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(AgentMessage).where(AgentMessage.project_id == self.project_id, AgentMessage.kind == "question")
            )).all()
            for m in rows:
                if -1 in (m.delivered_to or []):
                    continue
                out.append({"from": m.from_name, "text": m.text})
                m.delivered_to = [*(m.delivered_to or []), -1]  # -1 = the orchestrator
            await session.commit()
        return out

    def _roles_of(self, account_id: int) -> set[str]:
        roles = {t["role"] for t in self._running.values() if t["account_id"] == account_id}
        roles |= {t["role"] for t in self.completed if t.get("account_id") == account_id}
        acc = self.accounts.get(account_id)
        if acc is not None:
            roles.add(acc.default_role)
        return roles

    # ---- one round -----------------------------------------------------
    async def execute(self, plan: list[PlanItem], round_no: int) -> str | None:
        """Run a plan; returns None on success or a failure reason."""
        key_to_id: dict[str, int] = {}
        async with self.session_factory() as session:
            rows = []
            for item in plan:
                task = Task(project_id=self.project_id, role=item.role, task=item.task,
                            assigned_account_id=item.assign_to_account_id, status="pending",
                            branch=self.branch, plan_key=item.key, scope=item.scope, round=round_no)
                session.add(task)
                rows.append((item, task))
            await session.flush()
            for item, task in rows:
                key_to_id[item.key] = task.id
            for item, task in rows:
                task.depends_on = [key_to_id[d] for d in item.depends_on if d in key_to_id]
            await session.commit()
            specs = {task.id: {"id": task.id, "key": item.key, "role": item.role, "task": item.task,
                               "account_id": item.assign_to_account_id, "scope": item.scope,
                               "deps": set(task.depends_on or [])} for item, task in rows}
        for task_id in specs:
            await _set_task_status(self.session_factory, task_id, "pending", ws_manager=self.ws, project_id=self.project_id)
        return await self.run_specs(specs)

    async def run_specs(self, specs: dict[int, dict], done: set[int] | None = None) -> str | None:
        """Run task specs respecting dependencies; `done` = already finished task ids.
        A task that failed for a temporary reason is retried automatically."""
        pending = dict(specs)
        done = set(done or ())
        attempts: dict[int, int] = {t: specs[t].get("attempts", 0) for t in specs}
        running: dict[asyncio.Task, int] = {}
        failure: str | None = None
        while (pending or running) and failure is None:
            if not await _project_still_running(self.session_factory, self.project_id):
                failure = "Stopped by user"
                break
            busy = {specs[t]["account_id"] for t in running.values()}
            for task_id, spec in list(pending.items()):
                if spec["deps"] <= done and spec["account_id"] not in busy:
                    busy.add(spec["account_id"])
                    del pending[task_id]
                    self._running[task_id] = spec
                    running[asyncio.create_task(self._run_task(spec))] = task_id
            if not running:
                failure = "Some tasks can never start (their dependencies failed)."
                break
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for fut in finished:
                task_id = running.pop(fut)
                self._running.pop(task_id, None)
                try:
                    result = fut.result()
                except Exception as exc:  # unexpected bug in a task runner
                    logger.exception("Task %s crashed", task_id)
                    result = f"Task {task_id} crashed: {type(exc).__name__}: {exc}"
                if result is None:
                    done.add(task_id)
                    continue
                if failure is None and _retryable(result) and attempts[task_id] < TASK_RETRIES \
                        and await _project_still_running(self.session_factory, self.project_id):
                    attempts[task_id] += 1
                    await self._note_retry(task_id, attempts[task_id], result)
                    pending[task_id] = specs[task_id]
                    continue
                if failure is None:
                    failure = result
        if running:  # let in-flight agents finish (their work is not thrown away)
            for fut, task_id in list(running.items()):
                try:
                    if await fut is None:
                        done.add(task_id)
                except Exception:
                    logger.exception("Task %s crashed while stopping", task_id)
                self._running.pop(task_id, None)
        return failure

    async def _note_retry(self, task_id: int, attempt: int, reason: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is not None:
                task.attempts = attempt
                await session.commit()
        await _set_task_status(self.session_factory, task_id, "pending", ws_manager=self.ws, project_id=self.project_id)
        await self.ws.send_to_project(self.project_id, _event(
            "task_retry", self.project_id, task_id=task_id, status="pending",
            message=f"Task {task_id}: retry {attempt} of {TASK_RETRIES} ({reason[:200]})"))
        logger.info("Task %s: automatic retry %d (%s)", task_id, attempt, reason[:200])

    # ---- one task ------------------------------------------------------
    async def _run_task(self, spec: dict) -> str | None:
        task_id, task_role, task_text, scope = spec["id"], spec["role"], spec["task"], spec["scope"]
        ws, sf = self.ws, self.session_factory
        agent = self.accounts.get(spec["account_id"])
        if agent is None:
            async with sf() as session:
                agent = await session.get(Account, spec["account_id"])
        if agent is None:
            await _set_task_status(sf, task_id, "blocked", ws_manager=ws, project_id=self.project_id)
            return f"Task {task_id} has no assigned account (it may have been deleted)."

        agent_context = await _ensure_context(agent, session_factory=sf, browser_manager=self.browser_manager)
        if agent_context is None:
            await ws.send_to_project(self.project_id, _event(
                "session_lost", self.project_id, task_id=task_id, account_id=agent.id,
                message=f"Agent account {agent.id} has no live browser session"))
            await _set_task_status(sf, task_id, "blocked", ws_manager=ws, project_id=self.project_id)
            return f"Agent account {agent.id} ({agent.name}) has no live browser session. Log in again via VNC."

        await _set_task_status(sf, task_id, "running", ws_manager=ws, project_id=self.project_id)
        await ws.send_to_project(self.project_id, _event(
            "task_started", self.project_id, task_id=task_id, account_id=agent.id, status="running",
            message=f"Task {task_id} started ({task_role}, {agent.name})"))

        try:
            repo_files = await self.gh.list_tree(self.repo, self.branch)
            context_files = await _collect_context(self.gh, self.repo, self.branch, self._context_paths(spec, repo_files), repo_files)
        except GitHubError as exc:
            await _set_task_status(sf, task_id, "failed", ws_manager=ws, project_id=self.project_id)
            return f"GitHub: {exc.message}"

        peers = [{"name": getattr(self.accounts.get(p["account_id"]), "name", "?"), "role": p["role"],
                  "scope": p["scope"], "task": p["task"]}
                 for tid, p in self._running.items() if tid != task_id]
        agent_prompt = prompts.build_agent_prompt(
            project_name=self.snapshot["name"], github_repo=self.repo, role=task_role, task_text=task_text,
            task_id=task_id, branch=self.branch, project_task=self.snapshot["task"], repo_files=repo_files,
            context_files=context_files, previous=self.completed, scope=scope, peers=peers,
            inbox=await self.inbox_for(agent),
        )
        adapter = self.make_adapter()  # one adapter per task: callbacks must not be shared
        chat = _Chat(
            adapter, agent_context, chat_ref=f"project-{self.project_id}-task-{task_id}",
            timeout=self.settings.AGENT_TIMEOUT_SECONDS,
            notify=_notifier(ws, user_id=self.snapshot["user_id"], project_id=self.project_id, account=agent,
                             session_factory=sf, task_id=task_id),
            should_continue=lambda: _project_still_running(sf, self.project_id),
            refresh_context=lambda: _ensure_context(agent, session_factory=sf, browser_manager=self.browser_manager),
        )
        output, prompt, last_error = None, agent_prompt, ""
        try:
            for attempt in range(1, AGENT_FORMAT_ATTEMPTS + 1):
                try:
                    reply = await chat.ask(prompt)
                except Exception as exc:
                    await _set_task_status(sf, task_id, "failed", ws_manager=ws, project_id=self.project_id)
                    return _explain(exc, f"Agent for task {task_id} ({agent.name})")
                try:
                    output = parse_agent_output(reply)
                    if output.status == "DONE":
                        check_scope(output, scope)
                    break
                except AgentOutputError as exc:
                    output, last_error = None, str(exc)
                    _save_reply(self.project_id, f"task-{task_id}-attempt-{attempt}", reply)
                    logger.info("Task %s: reply rejected (attempt %d): %s", task_id, attempt, last_error)
                    # Ask in the SAME chat: the agent keeps its work and just resends it.
                    prompt = prompts.build_fix_prompt(last_error)
        finally:
            await chat.close()
        if output is None:
            await _set_task_status(sf, task_id, "failed", ws_manager=ws, project_id=self.project_id)
            return f"Task {task_id}: the agent's reply did not follow the required format ({last_error})."

        for to, text in output.messages:
            await self.post_message(from_task_id=task_id, from_account=agent, to=to, text=text)
        for text in output.questions:
            await self.post_message(from_task_id=task_id, from_account=agent, to="orchestrator", text=text, kind="question")

        if output.status == "BLOCKED":
            await _save_task_result(sf, task_id, summary=output.summary, commit_sha=None)
            await _set_task_status(sf, task_id, "blocked", ws_manager=ws, project_id=self.project_id)
            return f"Task {task_id} ({task_role}) is BLOCKED: {output.summary}"

        files = dict(output.files)
        files[f"status/task-{task_id}.md"] = f"DONE\n\n[{task_role}] {output.summary}\n"
        first_line = (output.summary.splitlines() or [""])[0][:72]
        try:
            async with self._commit_lock:  # parallel agents share the branch: one commit at a time
                commit_sha = await self.gh.commit_files(
                    self.repo, self.branch, files,
                    message=f"task-{task_id} [{task_role}]: {first_line or task_text[:72]}")
        except GitHubError as exc:
            await _set_task_status(sf, task_id, "failed", ws_manager=ws, project_id=self.project_id)
            if any(p.startswith(".github/workflows/") for p in files) and "workflow" in exc.message.lower():
                return ("GitHub refused to write the CI workflow (.github/workflows/): give the token the permission "
                        "«Workflows: Read and write» (GitHub → Settings → Developer settings → Fine-grained tokens) "
                        "and start the project again.")
            return f"GitHub commit failed: {exc.message}"

        await _save_task_result(sf, task_id, summary=output.summary, commit_sha=commit_sha, files=sorted(output.files))
        await _set_task_status(sf, task_id, "done", ws_manager=ws, project_id=self.project_id)
        for path in output.files:
            if path in self.touched:
                self.touched.remove(path)
            self.touched.append(path)
        self.completed.append({
            "id": task_id, "key": spec["key"], "role": task_role, "task": task_text, "branch": self.branch,
            "summary": output.summary, "commit": commit_sha, "files": sorted(output.files),
            "account_id": agent.id, "agent": agent.name,
        })
        return None

    def _context_paths(self, spec: dict, repo_files: list[str]) -> list[str]:
        """Most relevant first: the contract, files from dependencies, own area, other recent work."""
        deps = spec["deps"]
        dep_files = [f for t in self.completed if t["id"] in deps for f in t["files"]]
        own = [f for f in repo_files if spec["scope"] and in_scope(f, spec["scope"])]
        ordered = ["docs/CONTRACTS.md", *dep_files, *own, *reversed(self.touched)]
        seen, out = set(), []
        for f in ordered:
            if f not in seen and f in repo_files:
                seen.add(f)
                out.append(f)
        return out[:40]


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
    resume: bool = False,
) -> str:
    """Run one project end-to-end (or resume it: finished tasks are kept).
    Returns the final project status."""
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
            "max_rounds": 3 if project.max_rounds is None else project.max_rounds,
            "ci": project.ci_enabled is not False,
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
    # Resume after a failure: keep the plan and the finished tasks.
    resume_state = await _load_resume(session_factory, project_id, assignment_accounts) if resume else None
    plan = None
    if resume_state is None:
        split_prompt = prompts.build_split_prompt(
            project_name=project_snapshot["name"],
            description=project_snapshot["description"],
            task=project_snapshot["task"],
            github_repo=repo,
            accounts=account_rows,
            repo_files=repo_files,
            ci=project_snapshot["ci"],
        )
        plan_chat = _Chat(
            adapter, orchestrator_context, chat_ref=f"project-{project_id}",
            timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
            notify=_notifier(ws, user_id=project_snapshot["user_id"], project_id=project_id, account=orchestrator),
            should_continue=lambda: _project_still_running(session_factory, project_id),
        )
        try:
            raw_plan = await plan_chat.ask(split_prompt)
        except Exception as exc:
            await plan_chat.close()
            return await _fail_project(
                session_factory, project_id,
                _explain(exc, "Orchestrator"),
                ws_manager=ws,
            )

        plan = None
        for attempt in range(1, PLAN_FORMAT_ATTEMPTS + 1):
            try:
                plan = parse_task_plan(
                    raw_plan,
                    assignments=list(assignments),
                    orchestrator_account_id=project_snapshot["orchestrator_account_id"],
                )
                break
            except ValueError as exc:
                if attempt == PLAN_FORMAT_ATTEMPTS:
                    await plan_chat.close()
                    _save_reply(project_id, f"plan-attempt-{attempt}", raw_plan)
                    return await _fail_project(
                        session_factory, project_id,
                        f"Could not parse orchestrator plan: {exc}. Raw reply (truncated): {raw_plan[:500]}",
                        ws_manager=ws,
                    )
                logger.info("Project %s: plan not parsed (attempt %d): %s", project_id, attempt, exc)
                _save_reply(project_id, f"plan-attempt-{attempt}", raw_plan)
                try:
                    raw_plan = await plan_chat.ask(prompts.build_json_fix_prompt(str(exc)))
                except Exception as ask_exc:
                    await plan_chat.close()
                    return await _fail_project(session_factory, project_id, _explain(ask_exc, "Orchestrator"), ws_manager=ws)
        await plan_chat.close()
        for item in plan:
            if item.assign_to_account_id in project_roles and project_roles[item.assign_to_account_id] in AGENT_ROLES:
                item.role = project_roles[item.assign_to_account_id]
        logger.info("Project %s: orchestrator produced %d subtasks", project_id, len(plan))
    else:
        logger.info("Project %s: resuming round %d (%d finished, %d to run)", project_id,
                    resume_state["round"], len(resume_state["done_ids"]), len(resume_state["specs"]))
        await ws.send_to_project(project_id, _event(
            "project_resumed", project_id, status="running",
            message=f"Resumed: {len(resume_state['done_ids'])} task(s) kept, {len(resume_state['specs'])} to run"))

    # ---- rounds: run the plan in parallel, then the orchestrator reviews ---
    max_rounds = project_snapshot["max_rounds"]
    make_adapter = adapter_factory or load_adapter
    run = _Run(
        project_id=project_id, snapshot=project_snapshot, repo=repo, branch=work_branch, gh=gh, ws=ws,
        session_factory=session_factory, browser_manager=browser_manager, make_adapter=make_adapter,
        settings=settings, accounts=assignment_accounts, orchestrator=orchestrator,
    )
    round_no = resume_state["round"] if resume_state else 0
    if resume_state:
        run.completed = resume_state["completed"]
        run.touched = resume_state["touched"]
    ci_result = None
    first = True
    while True:
        if first and resume_state:
            failure = await run.run_specs(resume_state["specs"], done=resume_state["done_ids"])
        else:
            failure = await run.execute(plan, round_no)
        first = False
        if failure:
            return await _fail_project(session_factory, project_id, failure, ws_manager=ws)
        if project_snapshot["ci"]:
            ci_result = await _run_ci(gh, repo, work_branch, project_id=project_id, ws=ws,
                                      session_factory=session_factory, settings=settings)
            if ci_result.status == "stopped":
                return "failed"
        if round_no >= max_rounds:
            break
        if not await _project_still_running(session_factory, project_id):
            return "failed"
        try:
            repo_files = await gh.list_tree(repo, work_branch)
        except GitHubError as exc:
            return await _fail_project(session_factory, project_id, f"GitHub: {exc.message}", ws_manager=ws)
        questions = await run.take_questions()
        review_prompt = prompts.build_review_prompt(
            project_name=project_snapshot["name"], github_repo=repo, project_task=project_snapshot["task"],
            round_no=round_no + 1, max_rounds=max_rounds, completed=run.completed, repo_files=repo_files,
            questions=questions, accounts=account_rows,
            ci_text=ci_result.for_prompt() if ci_result else "",
        )
        try:
            raw_review = await _ask(
                make_adapter(), orchestrator_context,
                chat_ref=f"project-{project_id}-review-{round_no + 1}",
                prompt=review_prompt,
                timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
                notify=_notifier(ws, user_id=project_snapshot["user_id"], project_id=project_id, account=orchestrator),
                should_continue=lambda: _project_still_running(session_factory, project_id),
            )
            try:
                review = parse_review(raw_review, assignments=list(assignments),
                                      orchestrator_account_id=project_snapshot["orchestrator_account_id"],
                                      round_no=round_no + 1)
            except ValueError as first:
                raw_review = await _ask(
                    make_adapter(), orchestrator_context,
                    chat_ref=f"project-{project_id}-review-{round_no + 1}",
                    prompt=prompts.build_json_fix_prompt(str(first)) + "\n\nTHE ORIGINAL REQUEST:\n\n" + review_prompt,
                    timeout=settings.ORCHESTRATOR_TIMEOUT_SECONDS,
                    notify=_notifier(ws, user_id=project_snapshot["user_id"], project_id=project_id, account=orchestrator),
                    should_continue=lambda: _project_still_running(session_factory, project_id),
                )
                review = parse_review(raw_review, assignments=list(assignments),
                                      orchestrator_account_id=project_snapshot["orchestrator_account_id"],
                                      round_no=round_no + 1)
        except ValueError as exc:
            logger.warning("Project %s: review reply not usable (%s); finishing", project_id, exc)
            break
        except Exception as exc:
            return await _fail_project(session_factory, project_id, _explain(exc, "Orchestrator (review)"), ws_manager=ws)
        for to, text in review.answers:
            await run.post_message(from_task_id=None, from_account=None, to=to, text=text, kind="answer")
        if review.done:
            await ws.send_to_project(project_id, _event(
                "review_done", project_id, status="accepted", message=f"Orchestrator accepted the work (round {round_no + 1})"))
            break
        round_no += 1
        await ws.send_to_project(project_id, _event(
            "review_round", project_id, status="continue",
            message=f"Orchestrator requested {len(review.tasks)} fix task(s) (round {round_no})"))
        plan = review.tasks
    completed = run.completed

    # ---- final orchestrator summary + pull request -------------------------
    final_prompt = prompts.build_final_prompt(
        project_name=project_snapshot["name"],
        github_repo=repo,
        completed=completed,
        branch=work_branch,
        ci_text=ci_result.for_prompt() if ci_result else "",
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
            body=f"{summary}\n\n---\n{_ci_line(ci_result)}Commits made by AI Dev Department on behalf of the project agents.",
        )
    except GitHubError as exc:
        logger.warning("Project %s: pull request not created: %s", project_id, exc.message)
        summary += f"\n\n(Pull request was not created: {exc.message}. The work is on branch {work_branch}.)"

    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return "failed"
        project.status = "completed"
        project.result = summary + (f"\n\n{_ci_line(ci_result)}" if ci_result else "")
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


async def _save_task_result(session_factory, task_id: int, *, summary: str, commit_sha: str | None,
                            files: list[str] | None = None) -> None:
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is not None:
            task.summary = summary[:4000]
            task.commit_sha = commit_sha
            if files is not None:
                task.files = files
            await session.commit()


async def _collect_context(gh, repo: str, branch: str, paths: list[str], repo_files: list[str]) -> dict[str, str]:
    """File contents to show an agent, most relevant first (`paths`), then
    small top-level docs, within CONTEXT_BUDGET_BYTES."""
    wanted = list(paths)
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


async def _run_ci(gh, repo, branch, *, project_id, ws, session_factory, settings):
    from app.services.ci import wait_for_ci

    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is not None:
            project.ci_status = "running"
            await session.commit()
    await ws.send_to_project(project_id, _event("ci_waiting", project_id, status="running",
                                                message="Waiting for GitHub Actions"))
    result = await wait_for_ci(
        gh, repo, branch, timeout=settings.CI_TIMEOUT_SECONDS, poll=settings.CI_POLL_SECONDS,
        start_timeout=settings.CI_START_TIMEOUT_SECONDS,
        should_continue=lambda: _project_still_running(session_factory, project_id),
    )
    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is not None:
            project.ci_status, project.ci_url = result.status, result.url or None
            await session.commit()
    await ws.send_to_project(project_id, _event(
        "ci_result", project_id, status=result.status, message=f"CI: {result.status}",
        extra={"ci_status": result.status, "ci_url": result.url}))
    return result


def _ci_line(ci_result) -> str:
    if ci_result is None:
        return ""
    text = {"passed": "✅ CI passed", "failed": "❌ CI failed", "no_workflow": "CI: no workflow",
            "no_access": "CI: no access to GitHub Actions (token needs «Actions: Read»)"}.get(ci_result.status, f"CI: {ci_result.status}")
    return f"{text}{' — ' + ci_result.url if ci_result.url else ''}\n\n"


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
