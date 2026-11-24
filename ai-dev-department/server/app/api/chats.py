"""Chats with the API department.

GET    /api/chats                          -> [{id, title, status, updated_at}]
POST   /api/chats                          {prompt, mode?, model?} -> the chat; its first turn starts
GET    /api/chats/{id}                     -> chat + turns (prompt, status, result, agents, todos, changes)
POST   /api/chats/{id}/messages            {prompt, mode?, model?} -> a new turn (the department sees the chat)
POST   /api/chats/{id}/stop
PATCH  /api/chats/{id}                     {title}
DELETE /api/chats/{id}
GET    /api/chats/{id}/events?after=N      -> events of every turn after event N (live view)
GET    /api/chats/{id}/changes?run=        -> files changed by a turn, or by the whole chat
GET    /api/chats/{id}/diff?path=&run=     -> unified diff of one file
GET    /api/chats/{id}/files | /file?path= | /archive
mode: department (the lead hands work to sub-agents) | single (one agent) | plan (no changes)
model: "auto" | "<provider>:<model>" for the lead agent
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import department, snapshots
from app.agents.department import agents_tree
from app.agents.tools import SKIP_DIRS
from app.deps import get_current_user, get_db
from app.models import AgentRun, AgentRunEvent, Chat, User, utcnow

router = APIRouter(prefix="/api/chats", tags=["chats"])
MODES = ("department", "single", "plan")


class MessageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)
    mode: str = "department"
    model: str | None = Field(default=None, max_length=240)


class ChatPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _iso(d) -> str | None:
    return d.isoformat() + "Z" if d else None


async def _own(db: AsyncSession, user: User, chat_id: int) -> Chat:
    chat = await db.get(Chat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    return chat


async def _runs(db: AsyncSession, chat_id: int) -> list[AgentRun]:
    return list((await db.execute(select(AgentRun).where(AgentRun.chat_id == chat_id).order_by(AgentRun.id))).scalars())


def _chat_status(runs: list[AgentRun]) -> str:
    if not runs:
        return "idle"
    last = runs[-1]
    if department.is_running(last.id) or last.status in ("queued", "running"):
        return "running"
    return last.status


def _check(body: MessageIn) -> tuple[str, str | None]:
    mode = body.mode if body.mode in MODES else "department"
    model = (body.model or "").strip() or None
    if model in ("auto",):
        model = None
    if model and ":" not in model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model: «auto» or «provider:model»")
    return mode, model


async def _start_turn(db: AsyncSession, user: User, chat: Chat, body: MessageIn) -> AgentRun:
    mode, model = _check(body)
    try:
        run = await department.create_run(db, user.id, body.prompt, chat.title, "Orchestrator", chat=chat,
                                          mode=mode, model=model)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    chat.updated_at = utcnow()
    await db.commit()
    department.start(run.id)
    return run


@router.get("")
async def list_chats(user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    chats = list((await db.execute(select(Chat).where(Chat.user_id == user.id)
                                   .order_by(Chat.updated_at.desc()).limit(200))).scalars())
    out = []
    for c in chats:
        last = (await db.execute(select(AgentRun).where(AgentRun.chat_id == c.id)
                                 .order_by(AgentRun.id.desc()).limit(1))).scalar()
        out.append({"id": c.id, "title": c.title, "updated_at": _iso(c.updated_at),
                    "status": _chat_status([last] if last else [])})
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_chat(body: MessageIn, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    _check(body)
    title = " ".join(body.prompt.split())[:80] or "Новый чат"
    chat = Chat(user_id=user.id, title=title)
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    chat.workspace = str(department.chat_workspace(chat.id))
    shutil.rmtree(chat.workspace, ignore_errors=True)  # a folder left from an old database is not this chat's
    await db.commit()
    try:
        await _start_turn(db, user, chat, body)
    except HTTPException:
        await db.delete(chat)
        await db.commit()
        raise
    return {"id": chat.id, "title": chat.title}


def _turn(run: AgentRun, events: list[AgentRunEvent]) -> dict:
    todos = next((e.data.get("todos") for e in reversed(events) if e.type == "todos" and e.agent_id == "lead"), [])
    return {"id": run.id, "prompt": run.prompt, "status": run.status, "live": department.is_running(run.id),
            "mode": run.mode or "department", "model": run.model, "result": run.result, "error": run.error,
            "tokens": (run.tokens_in or 0) + (run.tokens_out or 0), "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
            "agents": agents_tree(events), "todos": todos or []}


@router.get("/{chat_id}")
async def get_chat(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                   db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    chat = await _own(db, user, chat_id)
    runs = await _runs(db, chat.id)
    events = list((await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id.in_([r.id for r in runs]))
                                    .order_by(AgentRunEvent.id))).scalars()) if runs else []
    by_run: dict[int, list] = {}
    for e in events:
        by_run.setdefault(e.run_id, []).append(e)
    return {"id": chat.id, "title": chat.title, "status": _chat_status(runs), "created_at": _iso(chat.created_at),
            "updated_at": _iso(chat.updated_at), "turns": [_turn(r, by_run.get(r.id, [])) for r in runs],
            "last_event": events[-1].id if events else 0}


@router.post("/{chat_id}/messages", status_code=status.HTTP_201_CREATED)
async def add_message(chat_id: int, body: MessageIn, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    chat = await _own(db, user, chat_id)
    if _chat_status(await _runs(db, chat.id)) == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "The department is still working on this chat: wait or stop it")
    run = await _start_turn(db, user, chat, body)
    return {"run_id": run.id}


@router.post("/{chat_id}/stop")
async def stop_chat(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    chat = await _own(db, user, chat_id)
    runs = await _runs(db, chat.id)
    stopped = False
    for r in runs:
        if department.stop(r.id):
            stopped = True
        elif r.status in ("queued", "running"):
            r.status = "stopped"
    await db.commit()
    return {"stopping": stopped}


@router.patch("/{chat_id}")
async def rename_chat(chat_id: int, body: ChatPatch, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    chat = await _own(db, user, chat_id)
    chat.title = body.title.strip()
    await db.commit()
    return {"id": chat.id, "title": chat.title}


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    chat = await _own(db, user, chat_id)
    runs = await _runs(db, chat.id)
    for r in runs:
        department.stop(r.id)
    ids = [r.id for r in runs]
    if ids:
        await db.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(ids)))
        await db.execute(delete(AgentRun).where(AgentRun.id.in_(ids)))
    ws = chat.workspace
    await db.delete(chat)
    await db.commit()
    if ws:
        shutil.rmtree(ws, ignore_errors=True)


@router.get("/{chat_id}/events")
async def chat_events(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[AsyncSession, Depends(get_db)],
                      after: Annotated[int, Query(ge=0)] = 0) -> list[dict]:
    chat = await _own(db, user, chat_id)
    ids = [r.id for r in await _runs(db, chat.id)]
    if not ids:
        return []
    events = (await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id.in_(ids), AgentRunEvent.id > after)
                               .order_by(AgentRunEvent.id).limit(1000))).scalars()
    return [{"id": e.id, "run_id": e.run_id, "agent_id": e.agent_id, "type": e.type, "data": e.data,
             "at": _iso(e.created_at)} for e in events]


async def _range(db: AsyncSession, chat: Chat, run_id: int | None) -> tuple[str | None, str | None]:
    runs = await _runs(db, chat.id)
    if not runs:
        return None, None
    if run_id:
        run = next((r for r in runs if r.id == run_id), None)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such turn in this chat")
        live = department.is_running(run.id)
        return run.base_commit, None if live else run.end_commit
    first = next((r.base_commit for r in runs if r.base_commit), None)
    return first, None


@router.get("/{chat_id}/changes")
async def chat_changes(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                       db: Annotated[AsyncSession, Depends(get_db)],
                       run: Annotated[int | None, Query()] = None) -> dict:
    chat = await _own(db, user, chat_id)
    base, end = await _range(db, chat, run)
    if not base:
        return {"files": [], "added": 0, "deleted": 0}
    try:
        files = await asyncio.to_thread(snapshots.changes, Path(chat.workspace), base, end)
    except snapshots.SnapshotError:
        files = []
    return {"files": files, "added": sum(f["added"] for f in files), "deleted": sum(f["deleted"] for f in files)}


@router.get("/{chat_id}/diff", response_class=PlainTextResponse)
async def chat_diff(chat_id: int, path: Annotated[str, Query(min_length=1)],
                    user: Annotated[User, Depends(get_current_user)], db: Annotated[AsyncSession, Depends(get_db)],
                    run: Annotated[int | None, Query()] = None):
    chat = await _own(db, user, chat_id)
    base, end = await _range(db, chat, run)
    if not base:
        return PlainTextResponse("")
    try:
        text = await asyncio.to_thread(snapshots.diff, Path(chat.workspace), base, path, end)
    except snapshots.SnapshotError:
        text = ""
    return PlainTextResponse(text[:400_000])


def _files(root: Path) -> list[Path]:
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in (".home", snapshots.STORE)]
        out += [Path(base) / f for f in files]
    return sorted(out)


@router.get("/{chat_id}/files")
async def chat_files(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    chat = await _own(db, user, chat_id)
    root = Path(chat.workspace)
    if not root.is_dir():
        return []
    return [{"path": str(p.relative_to(root)), "size": p.stat().st_size} for p in _files(root)[:3000]]


@router.get("/{chat_id}/file")
async def chat_file(chat_id: int, path: Annotated[str, Query(min_length=1)],
                    user: Annotated[User, Depends(get_current_user)], db: Annotated[AsyncSession, Depends(get_db)],
                    download: bool = False):
    chat = await _own(db, user, chat_id)
    root = Path(chat.workspace).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file() or snapshots.STORE in target.relative_to(root).parts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
    import mimetypes

    kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {"Content-Disposition": f'attachment; filename="{target.name}"'} if download else {}
    if kind == "text/html":  # shown in a sandboxed frame: no scripts of the app's origin
        headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups allow-modals"
    return Response(target.read_bytes()[:50_000_000], media_type=kind, headers=headers)


@router.get("/{chat_id}/archive")
async def chat_archive(chat_id: int, user: Annotated[User, Depends(get_current_user)],
                       db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    chat = await _own(db, user, chat_id)
    root = Path(chat.workspace)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if root.is_dir():
            for p in _files(root):
                zf.write(p, str(p.relative_to(root)))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="chat-{chat_id}.zip"'})
