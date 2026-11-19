"""Authentication: invite-only registration, login, refresh, me.

Rate limited via slowapi (see app.deps.limiter). No secrets are ever logged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db, limiter
from app.models import Invite, User
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserOut
from app.security import create_token_pair, decode_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def register(
    request: Request,
    data: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    invite = await db.scalar(select(Invite).where(Invite.code == data.invite_code.strip()))
    if invite is None or invite.used:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or already used invite code")
    if invite.expires_at is not None and invite.expires_at < _utc_naive():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invite code has expired")

    email = data.email.strip().lower()
    existing = await db.scalar(select(func.count()).select_from(User).where(User.email == email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")

    user = User(email=email, hashed_password=hash_password(data.password))
    db.add(user)
    await db.flush()

    invite.used = True
    invite.used_by = user.id
    invite.used_at = _utc_naive()

    await db.commit()
    await db.refresh(user)

    access_token, refresh_token = create_token_pair(user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(
    request: Request,
    data: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    email = data.email.strip().lower()
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(data.password, user.hashed_password):
        # Uniform error - do not reveal whether the email exists.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    access_token, refresh_token = create_token_pair(user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
async def refresh(
    request: Request,
    data: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    payload = decode_token(data.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed refresh token") from None

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")

    access_token, refresh_token = create_token_pair(user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.get("/me", response_model=UserOut)
@limiter.limit("60/minute")
async def me(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserOut:
    return UserOut.model_validate(current_user)
