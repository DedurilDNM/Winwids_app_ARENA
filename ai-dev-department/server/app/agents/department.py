"""The API department: a lead agent hands work to sub-agents of other roles.

A run has its own workspace folder. The lead (by default the Orchestrator) gets the
job; with the Task tool it starts sub-agents (several in one reply run in parallel, at
most AGENT_MAX_PARALLEL at a time). Every model call goes through the common key pool,
so a rate-limited key is replaced on the fly. Everything that happens is stored as
run events (agents, tool calls, files, texts, key rotations) for the live view.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.agents import llm
from app.agents.keypool import key_pool
from app.agents.llm import LLMError
from app.agents.providers import PROVIDERS
from app.agents.runtime import AgentSpec, run_agent
from app.agents.skills import skill_set
from app.agents.tools import ToolContext, ToolError
from app.config import DATA_DIR, get_settings
from app.models import ACCOUNT_ROLES, AgentRole, AgentRun, AgentRunEvent, ApiKey, utcnow

logger = logging.getLogger(__name__)

ROLE_TOOLS = {
    "Orchestrator": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Skill", "SendFile", "Screenshot"],
    "Architect": ["Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "Skill", "TodoWrite", "SendFile", "Screenshot"],
    "Coder": ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "Skill", "TodoWrite", "SendFile", "Screenshot"],
    "Reviewer": ["Read", "Glob", "Grep", "Bash", "Skill", "Screenshot"],
    "Tester": ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "SendFile", "Screenshot"],
    "Integrator": ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "TodoWrite", "SendFile", "Screenshot"],
}
# the tools that existed before tools_seen was kept: a role saved then gets the newer ones
TOOLS_BEFORE_SEEN = {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "TodoWrite", "Skill", "SendFile", "Task"}
_RUN_TASKS: dict[int, asyncio.Task] = {}
_STOP: set[int] = set()


def _s(name: str, default):
    return getattr(get_settings(), name, default)


def workspace_for(run_id: int) -> Path:
    return DATA_DIR / "workspaces" / f"run-{int(run_id)}"


async def has_keys(db, user_id: int) -> bool:
    row = (await db.execute(select(ApiKey.id).where(ApiKey.user_id == user_id,
                                                    ApiKey.status.in_(("available", "limited"))).limit(1))).first()
    return row is not None


def _role_tools(r) -> list[str]:
    """The saved tools, plus the role's default tools added after it was saved (a tool the
    user unchecked stays off: it is in tools_seen)."""
    if r.tools is None:
        return list(ROLE_TOOLS[r.role])
    seen = set(r.tools_seen) if r.tools_seen else TOOLS_BEFORE_SEEN
    return list(r.tools) + [t for t in ROLE_TOOLS[r.role] if t not in r.tools and t not in seen]


async def role_specs(db, user_id: int) -> dict[str, AgentSpec]:
    """The roles that are set up (by hand or by the model picker). No vendor is assumed."""
    saved = {r.role: r for r in (await db.execute(select(AgentRole).where(AgentRole.user_id == user_id))).scalars()}
    specs: dict[str, AgentSpec] = {}
    for role in ACCOUNT_ROLES:
        r = saved.get(role)
        if r is not None:
            if not r.enabled:
                continue
            specs[role] = AgentSpec(role=role, provider=r.provider, model=r.model, effort=r.effort,
                                    tools=_role_tools(r),
                                    skills=list(r.skills) if r.skills is not None else None,
                                    instructions=r.instructions or "",
                                    max_turns=int(r.max_turns or _s("AGENT_MAX_TURNS", 60)),
                                    fallbacks=[f for f in (r.fallbacks or []) if f.get("provider") in PROVIDERS])
    return specs


def match_role(name: str, roles: dict[str, AgentSpec]) -> str | None:
    aliases = {"оркестратор": "Orchestrator", "архитектор": "Architect", "кодер": "Coder", "программист": "Coder",
               "разработчик": "Coder", "ревьюер": "Reviewer", "тестер": "Tester", "интегратор": "Integrator"}
    key = (name or "").strip()
    found = next((r for r in roles if r.lower() == key.lower()), None) or aliases.get(key.lower())
    return found if found in roles else None


@dataclass
class RunState:
    run_id: int
    user_id: int
    workspace: Path
    specs: dict[str, AgentSpec]
    sem: asyncio.Semaphore
    counters: dict[str, int] = field(default_factory=lambda: {"tokens_in": 0, "tokens_out": 0})
    children: dict[str, int] = field(default_factory=dict)

    async def alive(self) -> bool:
        return self.run_id not in _STOP

    async def event(self, agent_id: str, data: dict) -> None:
        kind = data.get("type", "event")
        if kind == "usage":
            self.counters["tokens_in"] += int(data.get("tokens_in") or 0)
            self.counters["tokens_out"] += int(data.get("tokens_out") or 0)
            return
        payload = {k: v for k, v in data.items() if k != "type"}
        from app.db import SessionLocal

        async with SessionLocal() as db:
            db.add(AgentRunEvent(run_id=self.run_id, agent_id=agent_id, type=kind, data=payload))
            await db.commit()

    def child_id(self, parent: str) -> str:
        n = self.children.get(parent, 0) + 1
        self.children[parent] = n
        return f"{parent}.{n}"


async def _run_node(st: RunState, role: str, prompt: str, agent_id: str, parent: str | None, depth: int,
                    description: str = "", *, spec: AgentSpec | None = None, history: list[dict] | None = None) -> str:
    spec = spec or st.specs[role]
    skills = skill_set(st.user_id, spec.skills)
    max_depth = int(_s("AGENT_MAX_DEPTH", 1))

    async def emit(e: dict) -> None:
        await st.event(agent_id, e)

    async def spawn(child_role: str, child_desc: str, child_prompt: str, child: str | None = None) -> str:
        resolved = match_role(child_role, st.specs)
        if resolved is None:
            raise ToolError(f"No such role {child_role!r}; roles: {', '.join(r for r in st.specs if r != role)}")
        child = child or st.child_id(agent_id)
        async with st.sem:  # at most AGENT_MAX_PARALLEL sub-agents at once
            try:
                text = await _run_node(st, resolved, child_prompt, child, agent_id, depth + 1, child_desc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one failed sub-agent does not end the run: the lead decides
                logger.warning("Run %s: sub-agent %s failed: %s", st.run_id, child, type(exc).__name__)
                return f"The {resolved} could not finish: {str(exc)[:300]}"
        return f"Report from the {resolved}:\n\n{text}"

    can_spawn = depth < max_depth and "Task" in spec.tools
    ctx = ToolContext(workspace=st.workspace, role=role, agent_id=agent_id, read_roots=skills.roots, skills=skills,
                      emit=emit, spawn=spawn if can_spawn else None,
                      reserve=(lambda: st.child_id(agent_id)) if can_spawn else None,
                      bash_timeout=float(_s("AGENT_BASH_TIMEOUT_SECONDS", 120)),
                      sandbox=str(_s("AGENT_BASH_SANDBOX", "auto")))
    ctx.available_roles = [r for r in st.specs if r != role]

    async def on_wait(provider: str) -> None:
        await emit({"type": "keys_waiting", "provider": provider})

    async def on_rotate(event: dict) -> None:
        await emit(event)

    async def complete(agent_spec, req):
        """The role's model; when all its provider's keys are limited (or the model is gone), the next
        fallback model takes over - for this call and the rest of the agent's work."""
        options = [{"provider": agent_spec.provider, "model": agent_spec.model}] + list(agent_spec.fallbacks)
        last_error = None
        for i, option in enumerate(options):
            more = i < len(options) - 1
            call = dataclasses.replace(req, model=option["model"])
            try:
                resp = await key_pool.complete(st.user_id, option["provider"], call, should_continue=st.alive,
                                               on_wait=on_wait, on_rotate=on_rotate,
                                               wait_limit=float(_s("AGENT_FALLBACK_AFTER_SECONDS", 60)) if more else None)
            except LLMError as exc:
                gone = exc.outcome == "request" and (exc.status in (404,) or "No usable" in str(exc))
                if more and (exc.outcome == "rate_limited" or gone):
                    last_error = exc
                    await emit({"type": "model_switch", "from": f"{option['provider']}:{option['model']}",
                                "to": f"{options[i + 1]['provider']}:{options[i + 1]['model']}",
                                "reason": llm.short_reason(exc.outcome, exc.status)})
                    continue
                raise
            if i:  # stay on the model that works
                agent_spec.provider, agent_spec.model = option["provider"], option["model"]
                agent_spec.fallbacks = options[i + 1:]
            return resp
        raise last_error  # pragma: no cover

    await emit({"type": "agent_start", "role": role, "parent": parent, "description": description,
                "provider": spec.provider, "model": spec.model})
    try:
        result = await run_agent(spec, ctx, prompt, complete, should_continue=st.alive, history=history)
    except asyncio.CancelledError:
        await emit({"type": "agent_done", "status": "stopped"})
        raise
    except Exception as exc:
        await emit({"type": "agent_done", "status": "failed", "error": str(exc)[:300]})
        raise
    await emit({"type": "agent_done", "status": "done", "turns": result.turns, "stopped": result.stopped,
                "tokens_in": result.tokens_in, "tokens_out": result.tokens_out, "summary": result.text[:2000]})
    return result.text


async def _finish(run_id: int, **fields) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            return
        for k, v in fields.items():
            setattr(run, k, v)
        run.finished_at = utcnow()
        await db.commit()


PLAN_TOOLS = ["Read", "Glob", "Grep", "WebFetch", "TodoWrite", "Skill"]
SOLO_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "TodoWrite", "Skill", "SendFile", "Screenshot"]
PLAN_NOTE = ("This turn is planning only: study the project and the request, then answer with a clear, "
             "numbered plan (what to change, where, how to verify). Do not change any files.")


async def _history(db, run: AgentRun) -> list[dict]:
    """Earlier turns of the chat as a conversation: the user's message and the department's answer."""
    if not run.chat_id:
        return []
    earlier = (await db.execute(select(AgentRun).where(AgentRun.chat_id == run.chat_id, AgentRun.id < run.id)
                                .order_by(AgentRun.id))).scalars()
    out: list[dict] = []
    for r in earlier:
        answer = r.result or (f"(this turn ended with an error: {r.error[:300]})" if r.error else
                              "(this turn was stopped before an answer)")
        out += [{"role": "user", "content": r.prompt}, {"role": "assistant", "content": [{"type": "text", "text": answer}]}]
    return out


def _lead_spec(specs: dict[str, AgentSpec], lead: str, mode: str, model: str | None) -> tuple[str, AgentSpec | None]:
    """The spec the run starts with, shaped by the chat's mode and chosen model."""
    base = specs.get(lead) or specs.get("Orchestrator") or next(iter(specs.values()), None)
    if base is None:
        return lead, None
    spec = dataclasses.replace(base, tools=list(base.tools), fallbacks=list(base.fallbacks))
    if mode == "single":  # one agent with every working tool, on the strongest (lead's) model
        coder = specs.get("Coder")
        spec = dataclasses.replace(spec, role="Coder", tools=list(coder.tools if coder else SOLO_TOOLS),
                                   skills=coder.skills if coder else None,
                                   instructions=coder.instructions if coder else "")
        spec.tools = [t for t in spec.tools if t != "Task"]
    elif mode == "plan":
        spec.tools = [t for t in PLAN_TOOLS if t in spec.tools or t in ("Read", "Glob", "Grep")]
        spec.instructions = f"{spec.instructions}\n\n{PLAN_NOTE}".strip()
    if model and ":" in model:
        provider, name = model.split(":", 1)
        if provider in PROVIDERS and name:
            spec.provider, spec.model, spec.fallbacks = provider, name, []
    return spec.role, spec


async def execute(run_id: int) -> None:
    """Run a stored job to the end (status, result, tokens and snapshots saved)."""
    from app.agents import snapshots
    from app.db import SessionLocal

    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            return
        specs = await role_specs(db, run.user_id)
        history = await _history(db, run)
        run.status, run.started_at = "running", utcnow()
        await db.commit()
        user_id, prompt, lead = run.user_id, run.prompt, run.lead_role
        mode, model, chat_id = run.mode or "department", run.model, run.chat_id
        ws = Path(run.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    st = RunState(run_id, user_id, ws, specs, asyncio.Semaphore(int(_s("AGENT_MAX_PARALLEL", 4))))
    if chat_id:
        try:
            await _prepare_repo(chat_id, user_id, ws, st)
        except Exception as exc:
            await _finish(run_id, status="failed", error=f"Repository: {str(exc)[:400]}")
            _RUN_TASKS.pop(run_id, None)
            return
    base = None
    try:
        base = await asyncio.to_thread(snapshots.snapshot, ws, f"before turn {run_id}")
        await _save_run(run_id, base_commit=base)
    except Exception:
        logger.warning("Run %s: no snapshot of the workspace", run_id, exc_info=True)
    try:
        if lead not in specs and not (model and ":" in model):
            # nothing set up yet: choose the models automatically, the strongest leads
            await st.event("lead", {"type": "auto_models", "status": "started"})
            from app.agents import model_picker

            async with SessionLocal() as db:
                result = await model_picker.pick(db, user_id)
                await model_picker.apply(db, user_id, result["assignments"])
                st.specs.update(await role_specs(db, user_id))
            await st.event("lead", {"type": "auto_models", "status": "done", "assignments": [
                {"role": a["role"], "provider": a["provider"], "model": a["model"]} for a in result["assignments"]]})
        role, spec = _lead_spec(st.specs, lead, mode, model)
        if spec is None:
            raise LLMError("request", "No working model found: add a key to the pool, or set the role by hand")
        from app.engine import claude_code

        if await claude_code.should_use(await _user_engine(user_id), spec):
            chat = await _get_chat(chat_id) if chat_id else None
            text = await claude_code.run_turn(st, role, spec, prompt, mode=mode, chat=chat, history=history)
        else:
            text = await _run_node(st, role, prompt, "lead", None, 0 if mode == "department" else 99, spec=spec,
                                   history=history)
        await _finish(run_id, status="done", result=text, **st.counters)
    except asyncio.CancelledError:
        await _finish(run_id, status="stopped", **st.counters)
    except Exception as exc:
        logger.warning("Run %s failed: %s", run_id, type(exc).__name__, exc_info=True)
        await _finish(run_id, status="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}", **st.counters)
    finally:
        if chat_id:
            try:
                await _commit_repo(chat_id, ws, prompt, st)
            except Exception:
                logger.warning("Run %s: the turn could not be committed", run_id, exc_info=True)
        try:
            end = await asyncio.to_thread(snapshots.snapshot, ws, f"after turn {run_id}")
            await _save_run(run_id, end_commit=end)
        except Exception:
            logger.debug("Run %s: no end snapshot", run_id, exc_info=True)
        if chat_id:
            await _touch_chat(chat_id)
        _STOP.discard(run_id)
        _RUN_TASKS.pop(run_id, None)


async def _user_engine(user_id: int) -> str | None:
    from app.db import SessionLocal
    from app.models import User

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        return user.engine if user else None


async def _get_chat(chat_id: int):
    from app.db import SessionLocal
    from app.models import Chat

    async with SessionLocal() as db:
        return await db.get(Chat, chat_id)


async def _prepare_repo(chat_id: int, user_id: int, ws: Path, st: RunState) -> None:
    """A chat on a GitHub repository: clone it before the first turn (on the chat's work branch)."""
    from app.agents import repo as repo_mod
    from app.api.github import user_token
    from app.db import SessionLocal
    from app.models import Chat

    async with SessionLocal() as db:
        chat = await db.get(Chat, chat_id)
        if chat is None or not chat.repo or repo_mod.is_repo(ws):
            return
        token = await user_token(db, user_id)
        name, branch, base = chat.repo, chat.branch, chat.base_branch
    await st.event("lead", {"type": "repo", "status": "cloning", "repo": name, "branch": branch})
    base = await asyncio.to_thread(repo_mod.clone, ws, name, branch, token, base)
    async with SessionLocal() as db:
        chat = await db.get(Chat, chat_id)
        chat.base_branch = base
        await db.commit()
    await st.event("lead", {"type": "repo", "status": "ready", "repo": name, "branch": branch, "base": base})


async def _commit_repo(chat_id: int, ws: Path, prompt: str, st: RunState) -> None:
    """Only a chat opened on a GitHub repository commits; a .git made by an agent stays its own."""
    from app.agents import repo as repo_mod
    from app.db import SessionLocal
    from app.models import Chat

    async with SessionLocal() as db:
        chat = await db.get(Chat, chat_id)
        if chat is None or not chat.repo:
            return
    if not repo_mod.is_repo(ws):
        return
    title = " ".join(prompt.split("\n\nПриложенные файлы")[0].split())[:72] or "changes"
    done = await asyncio.to_thread(repo_mod.commit_turn, ws, f"AIDD: {title}")
    if done:
        await st.event("lead", {"type": "repo", "status": "committed", "sha": done["sha"][:10], "files": done["files"]})


async def _save_run(run_id: int, **fields) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if run is not None:
            for k, v in fields.items():
                setattr(run, k, v)
            await db.commit()


async def _touch_chat(chat_id: int) -> None:
    from app.db import SessionLocal
    from app.models import Chat

    async with SessionLocal() as db:
        chat = await db.get(Chat, chat_id)
        if chat is not None:
            chat.updated_at = utcnow()
            await db.commit()


async def create_run(db, user_id: int, prompt: str, title: str = "", lead_role: str = "Orchestrator", *,
                     chat=None, mode: str = "department", model: str | None = None) -> AgentRun:
    specs = await role_specs(db, user_id)
    if lead_role not in specs and not await has_keys(db, user_id):
        raise ValueError("The key pool is empty: add a key of any aggregator first")
    run = AgentRun(user_id=user_id, prompt=prompt, title=(title or prompt.splitlines()[0])[:200], lead_role=lead_role,
                   chat_id=chat.id if chat is not None else None, mode=mode, model=model or None)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    if chat is not None:
        run.workspace = chat.workspace
    else:
        run.workspace = str(workspace_for(run.id))
        import shutil

        shutil.rmtree(run.workspace, ignore_errors=True)  # left from an old database: not this run's
    await db.commit()
    return run


def chat_workspace(chat_id: int) -> Path:
    return DATA_DIR / "workspaces" / f"chat-{int(chat_id)}"


def start(run_id: int) -> None:
    _STOP.discard(run_id)  # ids of deleted runs come back (SQLite): never start «stopped»
    task = asyncio.create_task(execute(run_id))
    _RUN_TASKS[run_id] = task


def stop(run_id: int) -> bool:
    task = _RUN_TASKS.get(run_id)
    if task is not None and not task.done():
        _STOP.add(run_id)
        task.cancel()
        return True
    return False


def is_running(run_id: int) -> bool:
    task = _RUN_TASKS.get(run_id)
    return task is not None and not task.done()


async def mark_interrupted() -> int:
    """At startup: runs that were going when the server stopped cannot continue."""
    from app.db import SessionLocal

    async with SessionLocal() as db:
        runs = list((await db.execute(select(AgentRun).where(AgentRun.status.in_(("queued", "running"))))).scalars())
        for run in runs:
            run.status, run.error, run.finished_at = "failed", "The server restarted during the run", utcnow()
        await db.commit()
    return len(runs)


def agents_tree(events: list[AgentRunEvent]) -> list[dict]:
    """Agents of a run from its events: id, parent, role, status, tokens, last action."""
    agents: dict[str, dict] = {}
    for ev in events:
        a = agents.get(ev.agent_id)
        if ev.type == "agent_start":
            agents[ev.agent_id] = {"id": ev.agent_id, "parent": ev.data.get("parent"), "role": ev.data.get("role"),
                                   "description": ev.data.get("description") or "", "model": ev.data.get("model"),
                                   "provider": ev.data.get("provider"), "status": "working", "tokens": 0,
                                   "last": "", "files": 0}
        elif a is None:
            continue
        elif ev.type == "agent_done":
            a["status"] = ev.data.get("status", "done")
            a["tokens"] = int(ev.data.get("tokens_in") or 0) + int(ev.data.get("tokens_out") or 0)
        elif ev.type == "tool":
            a["last"] = f"{ev.data.get('tool')} {ev.data.get('arg', '')}".strip()
        elif ev.type == "file":
            a["files"] += 1
        elif ev.type == "keys_waiting":
            a["last"] = "ждёт свободный ключ"
    return list(agents.values())
