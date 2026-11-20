"""WebSocket realtime channel: /ws/projects/{project_id}.

Auth: browsers cannot set headers on WS handshakes. Preferred: connect
without a token and send {"type": "auth", "token": "<access JWT>"} as the
first message within 10 s (the token never appears in access logs).
Legacy: ?token=... query parameter (logged by uvicorn - avoid). The token is validated (type
must be `access`) and the project must belong to the token's user.

On connect the client immediately receives a `snapshot` event with the
current project + tasks (so the timeline renders without a REST round-trip),
followed by live events: task_started, task_status_changed, project_started,
project_completed, project_failed, session_lost, session_relogged.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Project, ProjectAssignment, Task
from app.security import ACCESS, decode_token
from app.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws/projects", tags=["ws"])


async def _authenticate(websocket: WebSocket, project_id: int, token: str) -> int | None:
    """Return the user_id if the token is valid and owns the project, else None."""
    payload = decode_token(token) if token else None
    if payload is None or payload.get("type") != ACCESS:
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return None
    async with SessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None or project.user_id != user_id:
            return None
    return user_id


async def _snapshot(project_id: int) -> dict:
    async with SessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return {"type": "snapshot", "project_id": project_id, "project": None, "tasks": []}
        assignments = (
            await session.scalars(
                select(ProjectAssignment).where(ProjectAssignment.project_id == project_id)
            )
        ).all()
        tasks = (
            await session.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id))
        ).all()
        return {
            "type": "snapshot",
            "project_id": project_id,
            "project": {
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "task": project.task,
                "github_repo": project.github_repo,
                "orchestrator_account_id": project.orchestrator_account_id,
                "status": project.status,
                "result": project.result,
                "work_branch": project.work_branch,
                "pr_url": project.pr_url,
                "created_at": project.created_at.isoformat() if project.created_at else None,
                "started_at": project.started_at.isoformat() if project.started_at else None,
                "completed_at": project.completed_at.isoformat() if project.completed_at else None,
                "assignments": [
                    {"account_id": a.account_id, "role": a.role} for a in assignments
                ],
            },
            "tasks": [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "role": t.role,
                    "task": t.task,
                    "assigned_account_id": t.assigned_account_id,
                    "status": t.status,
                    "branch": t.branch,
                    "summary": t.summary,
                    "commit_sha": t.commit_sha,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
                for t in tasks
            ],
        }


@router.websocket("/{project_id}")
async def project_ws(websocket: WebSocket, project_id: int) -> None:
    token = websocket.query_params.get("token", "")
    legacy = bool(token)
    if legacy:  # legacy query-param auth: reject before accepting
        if await _authenticate(websocket, project_id, token) is None:
            # 4401 = app-level "unauthorized" (1008 is also valid; 4401 is clearer in logs).
            await websocket.close(code=4401)
            return
    else:
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        except Exception:
            await websocket.close(code=4401)
            return
        token = first.get("token", "") if isinstance(first, dict) and first.get("type") == "auth" else ""
        if not token or await _authenticate(websocket, project_id, token) is None:
            await websocket.close(code=4401)
            return

    await manager.connect(project_id, websocket, accept=legacy)
    try:
        await websocket.send_json(await _snapshot(project_id))
        while True:
            # Clients do not need to send anything; this loop only detects
            # disconnects and keeps the socket alive.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WS connection for project %s ended", project_id, exc_info=True)
    finally:
        await manager.disconnect(project_id, websocket)
