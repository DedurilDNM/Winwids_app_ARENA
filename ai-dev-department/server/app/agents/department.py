"""The API department: a lead agent hands work to sub-agents of other roles.

A run has its own workspace folder. The lead (by default the Orchestrator) gets the
job; with the Task tool it starts sub-agents (several in one reply run in parallel, at
most AGENT_MAX_PARALLEL at a time). Every model call goes through the common key pool,
so a rate-limited key is replaced on the fly. Everything that happens is stored as
run events (agents, tool calls, files, texts, key rotations) for the live view.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

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
    "Orchestrator": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Skill"],
    "Architect": ["Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "Skill", "TodoWrite"],
    "Coder": ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "Skill", "TodoWrite"],
    "Reviewer": ["Read", "Glob", "Grep", "Bash", "Skill"],
    "Tester": ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"],
    "Integrator": ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "TodoWrite"],
}
_PREFERRED_PROVIDERS = ("anthropic", "openrouter")
_RUN_TASKS: dict[int, asyncio.Task] = {}
_STOP: set[int] = set()


def _s(name: str, default):
    return getattr(get_settings(), name, default)


def workspace_for(run_id: int) -> Path:
    return DATA_DIR / "workspaces" / f"run-{int(run_id)}"


async def _providers_with_keys(db, user_id: int) -> list[str]:
    rows = (await db.execute(select(ApiKey.provider).where(ApiKey.user_id == user_id,
                                                           ApiKey.status.in_(("available", "limited"))))).scalars()
    have = set(rows)
    return [p for p in _PREFERRED_PROVIDERS if p in have] + sorted(have - set(_PREFERRED_PROVIDERS))


async def role_specs(db, user_id: int) -> dict[str, AgentSpec]:
    """Every role's spec: the user's settings, else a default on a provider that has keys."""
    saved = {r.role: r for r in (await db.execute(select(AgentRole).where(AgentRole.user_id == user_id))).scalars()}
    providers = await _providers_with_keys(db, user_id)
    default_provider = next((p for p in providers if PROVIDERS[p].default_model), None)
    specs: dict[str, AgentSpec] = {}
    for role in ACCOUNT_ROLES:
        r = saved.get(role)
        if r is not None:
            if not r.enabled:
                continue
            specs[role] = AgentSpec(role=role, provider=r.provider, model=r.model, effort=r.effort,
                                    tools=list(r.tools) if r.tools is not None else list(ROLE_TOOLS[role]),
                                    skills=list(r.skills) if r.skills is not None else None,
                                    instructions=r.instructions or "",
                                    max_turns=int(r.max_turns or _s("AGENT_MAX_TURNS", 60)))
        elif default_provider:
            specs[role] = AgentSpec(role=role, provider=default_provider, model=PROVIDERS[default_provider].default_model,
                                    tools=list(ROLE_TOOLS[role]), max_turns=int(_s("AGENT_MAX_TURNS", 60)))
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
                    description: str = "") -> str:
    spec = st.specs[role]
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

    async def complete(provider, req):
        return await key_pool.complete(st.user_id, provider, req, should_continue=st.alive, on_wait=on_wait,
                                       on_rotate=on_rotate)

    await emit({"type": "agent_start", "role": role, "parent": parent, "description": description,
                "provider": spec.provider, "model": spec.model})
    try:
        result = await run_agent(spec, ctx, prompt, complete, should_continue=st.alive)
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


async def execute(run_id: int) -> None:
    """Run a stored job to the end (status, result and tokens saved)."""
    from app.db import SessionLocal

    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            return
        specs = await role_specs(db, run.user_id)
        run.status, run.started_at = "running", utcnow()
        await db.commit()
        user_id, prompt, lead = run.user_id, run.prompt, run.lead_role
        ws = Path(run.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    st = RunState(run_id, user_id, ws, specs, asyncio.Semaphore(int(_s("AGENT_MAX_PARALLEL", 4))))
    try:
        if lead not in specs:
            raise LLMError("request", f"The role {lead} is not set up: choose a provider and a model for it")
        text = await _run_node(st, lead, prompt, "lead", None, 0)
        await _finish(run_id, status="done", result=text, **st.counters)
    except asyncio.CancelledError:
        await _finish(run_id, status="stopped", **st.counters)
    except Exception as exc:
        logger.warning("Run %s failed: %s", run_id, type(exc).__name__, exc_info=True)
        await _finish(run_id, status="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}", **st.counters)
    finally:
        _STOP.discard(run_id)
        _RUN_TASKS.pop(run_id, None)


async def create_run(db, user_id: int, prompt: str, title: str = "", lead_role: str = "Orchestrator") -> AgentRun:
    specs = await role_specs(db, user_id)
    if lead_role not in specs:
        raise ValueError(f"The role {lead_role} is not set up: add a key to the pool and choose a provider and a "
                         "model for the role")
    run = AgentRun(user_id=user_id, prompt=prompt, title=(title or prompt.splitlines()[0])[:200], lead_role=lead_role)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run.workspace = str(workspace_for(run.id))
    await db.commit()
    return run


def start(run_id: int) -> None:
    task = asyncio.create_task(execute(run_id))
    _RUN_TASKS[run_id] = task


def stop(run_id: int) -> bool:
    _STOP.add(run_id)
    task = _RUN_TASKS.get(run_id)
    if task is not None and not task.done():
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
