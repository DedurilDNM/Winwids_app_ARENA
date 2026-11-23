"""Accounts API: CRUD (max 10 per user), VNC login confirmation.

Security rules enforced here:
- the service's account password NEVER reaches this API (manual VNC login only);
- only the encrypted (Fernet) storage_state blob is persisted;
- all routes require a valid access token; accounts are strictly per-user.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.deps import get_current_user, get_db
from app.models import Account, Project, ProjectAssignment, Task, User
from app.schemas import AccountCreate, AccountCreateResponse, AccountOut, AccountUpdate
from app.security import encrypt_bytes

logger = logging.getLogger(__name__)
from app.services import vnc_manager as vnc_module
from app.services.browser_manager import BrowserUnavailableError, browser_manager
from app.services.vnc_manager import VncUnavailableError, vnc_manager

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

MAX_ACCOUNTS_PER_USER = 10


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_own_account(
    account_id: Annotated[int, PathParam(ge=1)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Account:
    account = await db.get(Account, account_id)
    if account is None or account.user_id != current_user.id:
        # Do not reveal whether the account exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account


def _serialize_create(account: Account, *, vnc_url: str | None, headless: bool | None, vnc_error: str | None) -> AccountCreateResponse:
    data = AccountOut.model_validate(account).model_dump()
    return AccountCreateResponse(**data, vnc_url=vnc_url, headless=headless, vnc_error=vnc_error)


@router.post("", response_model=AccountCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    data: AccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountCreateResponse:
    count = await db.scalar(
        select(func.count()).select_from(Account).where(Account.user_id == current_user.id)
    )
    if count and count >= MAX_ACCOUNTS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account limit reached ({MAX_ACCOUNTS_PER_USER} per user)",
        )

    account = Account(
        user_id=current_user.id,
        name=data.name.strip(),
        service_note=data.service_note.strip(),
        default_role=data.default_role,
        github_repos=data.github_repos,
        status="pending_login",
        login_email=(data.login_email or "").strip() or None,
        model=(data.model or "").strip() or None,
        login_password_encrypted=encrypt_bytes(data.login_password.encode()) if data.login_password else None,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    # Launch the browser + VNC session right away (spec: POST create -> noVNC URL).
    vnc_url: str | None = None
    headless: bool | None = None
    vnc_error: str | None = None
    try:
        session = await vnc_manager.start_session(account)
        headless = session.headless
        vnc_url = None if session.headless else vnc_manager.vnc_url_for(session)
    except (VncUnavailableError, BrowserUnavailableError) as exc:
        vnc_error = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        vnc_error = f"Unexpected VNC error: {exc}"

    return _serialize_create(account, vnc_url=vnc_url, headless=headless, vnc_error=vnc_error)


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AccountOut]:
    rows = (
        await db.scalars(
            select(Account).where(Account.user_id == current_user.id).order_by(Account.id)
        )
    ).all()
    return [AccountOut.model_validate(row) for row in rows]


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account: Annotated[Account, Depends(_get_own_account)],
) -> AccountOut:
    return AccountOut.model_validate(account)


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    data: AccountUpdate,
    account: Annotated[Account, Depends(_get_own_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    if data.name is not None:
        account.name = data.name.strip()
    if data.service_note is not None:
        account.service_note = data.service_note.strip()
    if data.default_role is not None:
        account.default_role = data.default_role
    if data.github_repos is not None:
        account.github_repos = data.github_repos
    if data.login_email is not None:
        account.login_email = data.login_email.strip() or None
    if data.model is not None:
        account.model = data.model.strip() or None
    if data.login_password is not None:
        account.login_password_encrypted = encrypt_bytes(data.login_password.encode()) if data.login_password else None
    if data.status is not None:
        if data.status == "active":
            raise HTTPException(status_code=400, detail="Status 'active' is set via confirm-login only")
        account.status = data.status
    await db.commit()
    await db.refresh(account)
    return AccountOut.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_account(
    account: Annotated[Account, Depends(_get_own_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    account_id = account.id
    # Stop VNC session and browser first (spec: cleanup on delete).
    await vnc_manager.stop_session(account_id, keep_browser=False)

    # Detach references instead of failing on FK constraints.
    await db.execute(
        Task.__table__.update().where(Task.assigned_account_id == account_id).values(assigned_account_id=None)
    )
    await db.execute(
        Project.__table__.update()
        .where(Project.orchestrator_account_id == account_id)
        .values(orchestrator_account_id=None)
    )
    await db.execute(
        ProjectAssignment.__table__.delete().where(ProjectAssignment.account_id == account_id)
    )

    await db.delete(account)
    await db.commit()


@router.post("/{account_id}/confirm-login", response_model=AccountOut)
async def confirm_login(
    account: Annotated[Account, Depends(_get_own_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    """User pressed "Готово" in the client: persist the session, keep the browser alive."""
    handle_context = browser_manager.get_context(account.id)
    if handle_context is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No live browser session for this account. Start a VNC session first (POST /api/accounts/{id}/vnc/start).",
        )

    try:
        storage_state = await browser_manager.export_storage_state(account.id)
    except BrowserUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # never a bare 500: say what to do
        logger.warning("confirm-login: could not read the session of account %s", account.id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not read the login from the account's browser ({type(exc).__name__}). "
                   "Open the browser again («Открыть браузер»), check that you are logged in and press «Готово».",
        ) from exc

    blob = json.dumps(storage_state, default=str).encode("utf-8")
    was_relogin = account.status == "relogin_needed"
    account.storage_state_encrypted = encrypt_bytes(blob)
    account.status = "active"
    await db.commit()
    await db.refresh(account)

    # Close only the noVNC path; the browser stays alive in the background (spec).
    try:
        await vnc_manager.stop_session(account.id, keep_browser=True)
    except Exception:  # the login is saved already: a viewer problem must not fail it
        logger.warning("confirm-login: closing the viewer of account %s failed", account.id, exc_info=True)

    if was_relogin:
        # The user re-logged in after a session loss: tell affected projects.
        from app.services.orchestrator import _affected_project_ids, _event
        from app.ws_manager import manager as ws_manager

        for pid in await _affected_project_ids(SessionLocal, account.id):
            await ws_manager.send_to_project(
                pid,
                _event(
                    "session_relogged", pid, account_id=account.id,
                    status="active",
                    message=f"Account {account.id} session recovered",
                ),
            )

    return AccountOut.model_validate(account)
