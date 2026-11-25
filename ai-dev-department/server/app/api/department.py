"""The API department: roles, skills and runs.

GET  /api/agent-roles                -> every role with its settings (defaults where not set)
PUT  /api/agent-roles/{role}         {provider, model, effort?, tools?, skills?, instructions?, max_turns?, enabled?}
DELETE /api/agent-roles/{role}       -> forget the role's settings
POST /api/agent-roles/auto           {check?} -> choose models automatically (background); GET -> progress/result
GET  /api/skills                     -> [{name, description, source}]
GET  /api/skills/{name}              -> {name, description, source, body}
POST /api/skills                     {text} (a SKILL.md) | multipart file (.zip or SKILL.md)
DELETE /api/skills/{name}            -> only the user's own skills
POST /api/runs                       {prompt, title?, lead_role?} -> started
GET  /api/runs                       -> recent runs
GET  /api/runs/{id}                  -> run + agents tree + latest plan
GET  /api/runs/{id}/events?after=N   -> events after id N
POST /api/runs/{id}/stop
GET  /api/runs/{id}/files            -> workspace file list
GET  /api/runs/{id}/file?path=       -> one file (text)
GET  /api/runs/{id}/archive          -> the workspace as .zip
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import department, skills as skills_mod
from app.agents.department import ROLE_TOOLS, agents_tree, role_specs
from app.agents.providers import PROVIDERS
from app.agents.tools import ALL_TOOL_NAMES, SKIP_DIRS
from app.deps import get_current_user, get_db
from app.models import ACCOUNT_ROLES, AgentRole, AgentRun, AgentRunEvent, User

router = APIRouter(tags=["department"])
EFFORTS = ("low", "medium", "high", "xhigh", "max")


# ------------------------------------------------------------------ roles

class RoleIn(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200)
    effort: str | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    instructions: str = Field(default="", max_length=8000)
    max_turns: int | None = Field(default=None, ge=1, le=500)
    enabled: bool = True


class EngineIn(BaseModel):
    engine: str = Field(pattern="^(auto|claude_code|native)$")


async def _engine_out(db: AsyncSession, user: User) -> dict:
    from app.engine import claude_code

    st = await claude_code.status()
    specs = await role_specs(db, user.id)
    lead = specs.get("Orchestrator")
    return {"engine": user.engine or "auto", "default": (department._s("ENGINE", "auto") or "auto"),
            "installed": st["installed"], "version": st.get("version"),
            "lead_ok": bool(lead and claude_code.anthropic_capable(lead)),
            "active": "claude_code" if await claude_code.should_use(user.engine, lead) else "native"}


@router.get("/api/engine")
async def get_engine(user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Which engine runs the chats: Claude Code (when installed) or the built-in agents."""
    return await _engine_out(db, user)


@router.put("/api/engine")
async def set_engine(body: EngineIn, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    user.engine = None if body.engine == "auto" else body.engine
    await db.commit()
    return await _engine_out(db, user)


@router.get("/api/agent-roles")
async def list_roles(user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    saved = {r.role: r for r in (await db.execute(select(AgentRole).where(AgentRole.user_id == user.id))).scalars()}
    specs = await role_specs(db, user.id)
    from app.agents.model_picker import ROLE_ORDER

    out = []
    for role in ROLE_ORDER:  # the lead first, then by importance (the order models are given in)
        r, spec = saved.get(role), specs.get(role)
        out.append({"role": role, "configured": r is not None, "enabled": r.enabled if r else True,
                    "provider": spec.provider if spec else (r.provider if r else None),
                    "model": spec.model if spec else (r.model if r else None),
                    "effort": spec.effort if spec else (r.effort if r else None),
                    "tools": spec.tools if spec else (r.tools if r and r.tools is not None else ROLE_TOOLS[role]),
                    "skills": spec.skills if spec else (r.skills if r else None),
                    "instructions": r.instructions if r else "", "max_turns": r.max_turns if r else None,
                    "fallbacks": (r.fallbacks or []) if r else [], "auto": bool(r.auto) if r else False,
                    "ready": spec is not None})
    return out


@router.put("/api/agent-roles/{role}")
async def save_role(role: str, body: RoleIn, user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    if role not in ACCOUNT_ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown role")
    if body.provider not in PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown provider")
    if body.effort and body.effort not in EFFORTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"effort: one of {', '.join(EFFORTS)}")
    if body.tools is not None and not set(body.tools) <= set(ALL_TOOL_NAMES):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown tool")
    r = (await db.execute(select(AgentRole).where(AgentRole.user_id == user.id, AgentRole.role == role))).scalar()
    if r is None:
        r = AgentRole(user_id=user.id, role=role, provider=body.provider, model=body.model)
        db.add(r)
    changed = (r.provider, r.model) != (body.provider, body.model.strip())
    r.provider, r.model, r.effort = body.provider, body.model.strip(), body.effort or None
    r.tools, r.skills, r.instructions = body.tools, body.skills, body.instructions
    r.tools_seen = list(ALL_TOOL_NAMES)  # a tool left unchecked now stays off when new tools come
    r.max_turns, r.enabled = body.max_turns, body.enabled
    if r.auto and changed:
        r.auto, r.fallbacks = False, None  # chosen by hand now
    await db.commit()
    return {"role": role, "saved": True}


_AUTO: dict[int, dict] = {}
_AUTO_TASKS: set = set()


class AutoIn(BaseModel):
    check: bool = True  # really call the best candidates (a few tokens each)


@router.post("/api/agent-roles/auto", status_code=status.HTTP_202_ACCEPTED)
async def auto_roles(body: AutoIn, user: Annotated[User, Depends(get_current_user)]) -> dict:
    """Choose the models: strongest working one leads, the other roles in descending strength.
    Runs in the background (checking models takes a while); poll GET for the result."""
    import asyncio

    from app.agents import model_picker
    from app.db import SessionLocal

    user_id = user.id
    job = _AUTO.get(user_id)
    if job and job["running"]:
        return job
    job = _AUTO[user_id] = {"running": True, "checked": 0, "working": 0, "result": None, "error": None}

    async def progress(working: int, checked: int) -> None:
        job.update(working=working, checked=checked)

    async def go() -> None:
        try:
            async with SessionLocal() as db:
                result = await model_picker.pick(db, user_id, check=body.check, on_progress=progress)
                if result["assignments"]:
                    await model_picker.apply(db, user_id, result["assignments"])
            job["result"] = result
        except Exception as exc:
            job["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            job["running"] = False

    task = asyncio.create_task(go())
    _AUTO_TASKS.add(task)
    task.add_done_callback(_AUTO_TASKS.discard)
    return job


@router.get("/api/agent-roles/auto")
async def auto_roles_status(user: Annotated[User, Depends(get_current_user)]) -> dict:
    return _AUTO.get(user.id) or {"running": False, "result": None, "error": None, "checked": 0, "working": 0}


@router.delete("/api/agent-roles/{role}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_role(role: str, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    """Forget the role's settings (the model picker or the user sets it again)."""
    await db.execute(delete(AgentRole).where(AgentRole.user_id == user.id, AgentRole.role == role))
    await db.commit()


# ------------------------------------------------------------------ skills

def _skill_out(s) -> dict:
    return {"name": s.name, "description": s.description, "source": s.source}


@router.get("/api/skills")
async def list_skills(user: Annotated[User, Depends(get_current_user)]) -> list[dict]:
    return [_skill_out(s) for s in skills_mod.all_skills(user.id)]


@router.get("/api/skills/{name}")
async def get_skill(name: str, user: Annotated[User, Depends(get_current_user)]) -> dict:
    s = next((x for x in skills_mod.all_skills(user.id) if x.name == name), None)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such skill")
    return {**_skill_out(s), "body": s.body()}


@router.post("/api/skills", status_code=status.HTTP_201_CREATED)
async def add_skill(request: Request, user: Annotated[User, Depends(get_current_user)]) -> dict:
    try:
        if request.headers.get("content-type", "").startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise skills_mod.SkillError("send a file")
            data = await upload.read()
            skill = (skills_mod.install_zip(user.id, data) if data[:2] == b"PK"
                     else skills_mod.install_text(user.id, data.decode("utf-8")))
        else:
            body = await request.json()
            skill = skills_mod.install_text(user.id, str((body or {}).get("text") or ""))
    except skills_mod.SkillError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except UnicodeDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SKILL.md must be UTF-8 text") from None
    return _skill_out(skill)


class SkillImportIn(BaseModel):
    url: str = Field(min_length=10, max_length=500)


@router.post("/api/skills/import", status_code=status.HTTP_201_CREATED)
async def import_skill(body: SkillImportIn, user: Annotated[User, Depends(get_current_user)]) -> dict:
    """A skill folder from a GitHub link (with its scripts and references)."""
    from app.api.github import user_token
    from app.db import SessionLocal

    async with SessionLocal() as db:  # a private repository: the user's GitHub token, when connected
        token = await user_token(db, user.id)
    try:
        installed = await skills_mod.import_from_url(user.id, body.url, token)
    except skills_mod.SkillError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return {"installed": [_skill_out(s) for s in installed]}


@router.delete("/api/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(name: str, user: Annotated[User, Depends(get_current_user)]) -> None:
    if not skills_mod.remove(user.id, name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not one of your skills (built-in skills stay)")


# ------------------------------------------------------------------ runs

class RunIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=50_000)
    title: str = Field(default="", max_length=200)
    lead_role: str = "Orchestrator"


def _run_out(run: AgentRun) -> dict:
    return {"id": run.id, "title": run.title, "prompt": run.prompt, "lead_role": run.lead_role, "status": run.status,
            "result": run.result, "error": run.error, "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
            "created_at": _iso(run.created_at), "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at), "live": department.is_running(run.id)}


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() + "Z" if d else None


async def _own_run(db: AsyncSession, user: User, run_id: int) -> AgentRun:
    run = await db.get(AgentRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.post("/api/runs", status_code=status.HTTP_201_CREATED)
async def create_run(body: RunIn, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    if body.lead_role not in ACCOUNT_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role")
    try:
        run = await department.create_run(db, user.id, body.prompt, body.title, body.lead_role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    department.start(run.id)
    return _run_out(run)


@router.get("/api/runs")
async def list_runs(user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    runs = (await db.execute(select(AgentRun).where(AgentRun.user_id == user.id)
                             .order_by(AgentRun.id.desc()).limit(50))).scalars()
    return [_run_out(r) for r in runs]


@router.get("/api/runs/{run_id}")
async def get_run(run_id: int, user: Annotated[User, Depends(get_current_user)],
                  db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    run = await _own_run(db, user, run_id)
    events = list((await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id)
                                    .order_by(AgentRunEvent.id))).scalars())
    todos = next((e.data.get("todos") for e in reversed(events) if e.type == "todos" and e.agent_id == "lead"), [])
    return {**_run_out(run), "agents": agents_tree(events), "todos": todos or [],
            "last_event": events[-1].id if events else 0}


@router.get("/api/runs/{run_id}/events")
async def run_events(run_id: int, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)],
                     after: Annotated[int, Query(ge=0)] = 0) -> list[dict]:
    await _own_run(db, user, run_id)
    events = (await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id, AgentRunEvent.id > after)
                               .order_by(AgentRunEvent.id).limit(500))).scalars()
    return [{"id": e.id, "agent_id": e.agent_id, "type": e.type, "data": e.data, "at": _iso(e.created_at)}
            for e in events]


@router.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: int, user: Annotated[User, Depends(get_current_user)],
                   db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    run = await _own_run(db, user, run_id)
    stopped = department.stop(run_id)
    if not stopped and run.status in ("queued", "running"):
        run.status = "stopped"
        await db.commit()
    return {"stopping": stopped}


@router.delete("/api/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: int, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    run = await _own_run(db, user, run_id)
    department.stop(run_id)
    import shutil

    shutil.rmtree(run.workspace, ignore_errors=True)
    await db.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
    await db.delete(run)
    await db.commit()


def _files(root: Path) -> list[Path]:
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in (".home", ".aidd-git")]
        out += [Path(base) / f for f in files]
    return sorted(out)


@router.get("/api/runs/{run_id}/files")
async def run_files(run_id: int, user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    run = await _own_run(db, user, run_id)
    root = Path(run.workspace)
    if not root.is_dir():
        return []
    return [{"path": str(p.relative_to(root)), "size": p.stat().st_size} for p in _files(root)[:2000]]


@router.get("/api/runs/{run_id}/file", response_class=PlainTextResponse)
async def run_file(run_id: int, path: Annotated[str, Query(min_length=1)],
                   user: Annotated[User, Depends(get_current_user)], db: Annotated[AsyncSession, Depends(get_db)]):
    run = await _own_run(db, user, run_id)
    root = Path(run.workspace).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file() or ".aidd-git" in target.relative_to(root).parts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
    data = target.read_bytes()[:2_000_000]
    return PlainTextResponse(data.decode("utf-8", "replace"))


@router.get("/api/runs/{run_id}/archive")
async def run_archive(run_id: int, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    run = await _own_run(db, user, run_id)
    root = Path(run.workspace)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if root.is_dir():
            for p in _files(root):
                zf.write(p, str(p.relative_to(root)))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="run-{run_id}.zip"'})
