"""Projects API: CRUD + start/stop.

A project binds a task description + GitHub repo to an orchestrator account
and a set of role assignments. POST /{id}/start validates that every involved
account is owned by the caller and `active`, flips the project to `running`
and enqueues the arq job `run_project` (see app.workers.run_project).

Ownership rule (same as accounts): foreign projects are invisible (404).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models import Account, AgentMessage, Project, ProjectAssignment, Task, User
from app.schemas import (
    AgentMessageOut,
    ProjectAssignmentIn,
    ProjectCreate,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdate,
    TaskOut,
)
from app.workers.run_project import enqueue_run_project

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_own_project(
    project_id: Annotated[int, PathParam(ge=1)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Project:
    project = await db.get(Project, project_id)
    if project is None or project.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _validate_account(
    db: AsyncSession, user_id: int, account_id: int | None, *, field: str
) -> Account | None:
    if account_id is None:
        return None
    account = await db.get(Account, account_id)
    if account is None or account.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{field}: account {account_id} not found",
        )
    return account


async def _serialize(project: Project, db: AsyncSession, *, with_tasks: bool = False) -> ProjectOut:
    assignments = (
        await db.scalars(
            select(ProjectAssignment)
            .where(ProjectAssignment.project_id == project.id)
            .order_by(ProjectAssignment.id)
        )
    ).all()
    base = ProjectOut.model_validate(project).model_dump()
    base["assignments"] = [
        ProjectAssignmentIn(account_id=a.account_id, role=a.role)  # type: ignore[arg-type]
        for a in assignments
    ]
    if with_tasks:
        tasks = (
            await db.scalars(
                select(Task).where(Task.project_id == project.id).order_by(Task.id)
            )
        ).all()
        base["tasks"] = [TaskOut.model_validate(t) for t in tasks]
        messages = (
            await db.scalars(
                select(AgentMessage).where(AgentMessage.project_id == project.id).order_by(AgentMessage.id)
            )
        ).all()
        base["messages"] = [AgentMessageOut.model_validate(m) for m in messages]
        return ProjectDetailOut(**base)
    return ProjectOut(**base)


async def _check_can_run(project: Project, db: AsyncSession) -> None:
    """The orchestrator and every assigned account must exist and be logged in."""
    if project.orchestrator_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project has no orchestrator account. Set orchestrator_account_id first.",
        )
    orchestrator = await _validate_account(
        db, project.user_id, project.orchestrator_account_id, field="orchestrator_account_id"
    )
    if orchestrator is not None and orchestrator.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Orchestrator account {orchestrator.id} is not active (status={orchestrator.status}). Log in via VNC first.",
        )

    assignments = (
        await db.scalars(
            select(ProjectAssignment).where(ProjectAssignment.project_id == project.id)
        )
    ).all()
    if not assignments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project has no agent assignments. Add at least one assignment.",
        )
    for assignment in assignments:
        account = await db.get(Account, assignment.account_id)
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Assigned account {assignment.account_id} no longer exists",
            )
        if account.status != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Assigned account {account.id} is not active (status={account.status}). Log in via VNC first.",
            )


def _dedupe_assignments(items: list[ProjectAssignmentIn]) -> list[ProjectAssignmentIn]:
    seen: set[int] = set()
    result: list[ProjectAssignmentIn] = []
    for item in items:
        if item.account_id in seen:
            continue
        seen.add(item.account_id)
        result.append(item)
    return result


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectOut:
    await _validate_account(db, current_user.id, data.orchestrator_account_id, field="orchestrator_account_id")
    assignments = _dedupe_assignments(data.assignments)
    for item in assignments:
        await _validate_account(db, current_user.id, item.account_id, field="assignments")

    project = Project(
        user_id=current_user.id,
        name=data.name.strip(),
        description=data.description.strip(),
        task=data.task.strip(),
        github_repo=data.github_repo.strip(),
        orchestrator_account_id=data.orchestrator_account_id,
        max_rounds=data.max_rounds,
        ci_enabled=data.ci_enabled,
        status="draft",
    )
    db.add(project)
    await db.flush()
    for item in assignments:
        db.add(ProjectAssignment(project_id=project.id, account_id=item.account_id, role=item.role))
    await db.commit()
    await db.refresh(project)
    return await _serialize(project, db)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ProjectOut]:
    rows = (
        await db.scalars(
            select(Project).where(Project.user_id == current_user.id).order_by(Project.id.desc())
        )
    ).all()
    return [await _serialize(row, db) for row in rows]


@router.get("/{project_id}", response_model=ProjectDetailOut)
async def get_project(
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectDetailOut:
    return await _serialize(project, db, with_tasks=True)  # type: ignore[return-value]


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    data: ProjectUpdate,
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectOut:
    if project.status == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot edit a running project")
    provided = data.model_fields_set
    if "name" in provided and data.name is not None:
        project.name = data.name.strip()
    if "description" in provided and data.description is not None:
        project.description = data.description.strip()
    if "task" in provided and data.task is not None:
        project.task = data.task.strip()
    if "github_repo" in provided and data.github_repo is not None:
        project.github_repo = data.github_repo.strip()
    if "orchestrator_account_id" in provided:
        await _validate_account(db, project.user_id, data.orchestrator_account_id, field="orchestrator_account_id")
        project.orchestrator_account_id = data.orchestrator_account_id
    if "assignments" in provided and data.assignments is not None:
        assignments = _dedupe_assignments(data.assignments)
        for item in assignments:
            await _validate_account(db, project.user_id, item.account_id, field="assignments")
        await db.execute(
            ProjectAssignment.__table__.delete().where(ProjectAssignment.project_id == project.id)
        )
        for item in assignments:
            db.add(ProjectAssignment(project_id=project.id, account_id=item.account_id, role=item.role))
    await db.commit()
    await db.refresh(project)
    return await _serialize(project, db)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_project(
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if project.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a running project. Stop it first (POST /api/projects/{id}/stop).",
        )
    await db.execute(ProjectAssignment.__table__.delete().where(ProjectAssignment.project_id == project.id))
    await db.execute(Task.__table__.delete().where(Task.project_id == project.id))
    await db.delete(project)
    await db.commit()


@router.post("/{project_id}/start", response_model=ProjectOut, status_code=status.HTTP_202_ACCEPTED)
async def start_project(
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectOut:
    if project.status == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project is already running")
    if project.status == "completed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project is already completed")

    await _check_can_run(project, db)

    # A re-run starts from a clean task list and message board (history stays in git).
    await db.execute(Task.__table__.delete().where(Task.project_id == project.id))
    await db.execute(AgentMessage.__table__.delete().where(AgentMessage.project_id == project.id))
    project.status = "running"
    project.started_at = _utc_naive()
    project.completed_at = None
    project.result = None
    project.pr_url = None
    project.ci_status = None
    project.ci_url = None
    await db.commit()
    await db.refresh(project)

    await enqueue_run_project(project.id)
    return await _serialize(project, db)


@router.post("/{project_id}/resume", response_model=ProjectOut, status_code=status.HTTP_202_ACCEPTED)
async def resume_project(
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectOut:
    """Continue a stopped project from where it stopped: finished tasks are
    kept, the unfinished ones of the latest round run again."""
    if project.status != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a stopped project can be resumed")
    await _check_can_run(project, db)
    project.status = "running"
    project.started_at = _utc_naive()
    project.completed_at = None
    project.result = None
    project.ci_status = None
    await db.commit()
    await db.refresh(project)
    await enqueue_run_project(project.id, resume=True)
    return await _serialize(project, db)


@router.post("/{project_id}/stop", response_model=ProjectOut)
async def stop_project(
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectOut:
    """Cooperative stop: flips status to failed; the worker aborts at the next checkpoint."""
    if project.status != "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project is not running")
    project.status = "failed"
    project.result = "Stopped by user"
    project.completed_at = _utc_naive()
    await db.commit()
    await db.refresh(project)
    return await _serialize(project, db)


@router.get("/{project_id}/tasks", response_model=list[TaskOut])
async def list_tasks(
    project: Annotated[Project, Depends(_get_own_project)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TaskOut]:
    rows = (
        await db.scalars(
            select(Task).where(Task.project_id == project.id).order_by(Task.id)
        )
    ).all()
    return [TaskOut.model_validate(row) for row in rows]
