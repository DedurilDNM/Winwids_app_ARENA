"""GitHub connection for the server-side commits.

PUT    /api/github             {token}  -> validate (GET /user) and store encrypted
GET    /api/github                      -> {connected, login, token_hint}
DELETE /api/github                      -> forget the token
GET    /api/github/repo-check?repo=o/n  -> can the token push to this repository?

The token is stored Fernet-encrypted (ConnectorCredential kind="github"),
never returned and never logged; only its last 4 characters are shown.
"""

from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.github import GitHubConnector, GitHubError
from app.deps import get_current_user, get_db
from app.models import ConnectorCredential, User
from app.security import decrypt_bytes, encrypt_bytes

router = APIRouter(prefix="/api/github", tags=["github"])

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def connector_for(token: str) -> GitHubConnector:
    """Factory; tests replace it with an in-memory GitHub."""
    return GitHubConnector(token=token)


class GitHubTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=255)


class GitHubStatus(BaseModel):
    connected: bool
    login: str | None = None
    token_hint: str | None = None


class RepoCheck(BaseModel):
    repo: str
    ok: bool
    can_push: bool = False
    default_branch: str | None = None
    private: bool | None = None
    actions: bool | None = None  # token can read GitHub Actions results (CI)
    message: str


async def _credential(db: AsyncSession, user_id: int) -> ConnectorCredential | None:
    return (
        await db.scalars(
            select(ConnectorCredential)
            .where(ConnectorCredential.user_id == user_id, ConnectorCredential.kind == "github")
            .order_by(ConnectorCredential.id.desc())
        )
    ).first()


async def user_token(db: AsyncSession, user_id: int) -> str | None:
    """The user's GitHub token (for the server's own git and API calls), or None."""
    row = await _credential(db, user_id)
    return (_payload(row).get("token") or None) if row is not None else None


def _payload(row: ConnectorCredential) -> dict:
    raw = decrypt_bytes(row.encrypted_payload).decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"token": raw}
    except ValueError:
        return {"token": raw}


@router.get("", response_model=GitHubStatus)
async def github_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GitHubStatus:
    row = await _credential(db, current_user.id)
    if row is None:
        return GitHubStatus(connected=False)
    data = _payload(row)
    token = str(data.get("token") or "")
    return GitHubStatus(connected=True, login=data.get("login") or row.label or None, token_hint=f"…{token[-4:]}" if token else None)


@router.put("", response_model=GitHubStatus)
async def connect_github(
    data: GitHubTokenIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GitHubStatus:
    token = data.token.strip()
    try:
        user = await connector_for(token).get_user()
    except GitHubError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    login = str(user.get("login") or "")
    await db.execute(
        delete(ConnectorCredential).where(
            ConnectorCredential.user_id == current_user.id, ConnectorCredential.kind == "github"
        )
    )
    db.add(ConnectorCredential(
        user_id=current_user.id,
        kind="github",
        label=login,
        encrypted_payload=encrypt_bytes(json.dumps({"token": token, "login": login}).encode()),
    ))
    await db.commit()
    return GitHubStatus(connected=True, login=login, token_hint=f"…{token[-4:]}")


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def disconnect_github(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await db.execute(
        delete(ConnectorCredential).where(
            ConnectorCredential.user_id == current_user.id, ConnectorCredential.kind == "github"
        )
    )
    await db.commit()


@router.get("/repo-check", response_model=RepoCheck)
async def repo_check(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    repo: Annotated[str, Query(min_length=3, max_length=200)],
) -> RepoCheck:
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not REPO_RE.match(repo):
        raise HTTPException(status_code=400, detail="Repository must look like owner/name")
    row = await _credential(db, current_user.id)
    if row is None:
        raise HTTPException(status_code=409, detail="GitHub is not connected")
    gh = connector_for(str(_payload(row).get("token") or ""))
    try:
        meta = await gh.get_repo(repo)
    except GitHubError as exc:
        return RepoCheck(repo=repo, ok=False, message=exc.message)
    can_push = bool((meta.get("permissions") or {}).get("push"))
    branch = meta.get("default_branch")
    has_commits = bool(branch) and await gh.get_branch_sha(repo, branch) is not None
    actions = await gh.actions_readable(repo)
    if not can_push:
        msg = "Read-only access: give the token 'Contents' and 'Pull requests' read and write"
    elif not has_commits:
        msg = "The repository is empty: add a README (any first commit) on GitHub"
    elif not actions:
        msg = "OK for commits; for CI add the token permission 'Actions: Read'"
    else:
        msg = "OK: the server can commit to this repository"
    return RepoCheck(
        repo=repo, ok=can_push and has_commits, can_push=can_push,
        default_branch=branch, private=meta.get("private"), actions=actions, message=msg,
    )


@router.get("/repos")
async def list_repos(current_user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    """Repositories the connected token can see (for «work in a repository» in a chat)."""
    token = await user_token(db, current_user.id)
    if not token:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitHub is not connected")
    try:
        return await connector_for(token).list_repos()
    except GitHubError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None

