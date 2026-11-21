"""GET /api/attention: what needs the user right now (captcha / pop-up)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps import get_current_user
from app.models import User
from app.services.attention import list_attention

router = APIRouter(prefix="/api/attention", tags=["attention"])


@router.get("")
async def attention(current_user: Annotated[User, Depends(get_current_user)]) -> list[dict]:
    return list_attention(current_user.id)
