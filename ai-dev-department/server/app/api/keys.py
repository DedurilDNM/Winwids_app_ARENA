"""The common key pool («котёл ключей»).

GET    /api/keys/providers          -> the provider catalogue
GET    /api/keys                    -> the user's keys (masked) + a summary
POST   /api/keys                    {provider, key, label?, base_url?} -> checked, then stored encrypted
PATCH  /api/keys/{id}               {label?, disabled?}
DELETE /api/keys/{id}
POST   /api/keys/{id}/check         -> probe now; the status is updated
GET    /api/keys/models?provider=   -> model ids from the provider (via any working key)

A key is never returned or logged: only its last 4 characters.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import llm
from app.agents.keypool import key_pool, next_check_in
from app.agents.providers import PROVIDERS, get_provider, public_list
from app.deps import get_current_user, get_db
from app.models import ApiKey, User, utcnow
from app.security import decrypt_str, encrypt_str

router = APIRouter(prefix="/api/keys", tags=["keys"])


class KeyIn(BaseModel):
    provider: str
    key: str = Field(min_length=8, max_length=500)
    label: str = Field(default="", max_length=120)
    base_url: str | None = Field(default=None, max_length=300)


class KeyPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    disabled: bool | None = None


class KeyOut(BaseModel):
    id: int
    provider: str
    provider_name: str
    label: str
    tail: str
    base_url: str | None
    status: str
    reason: str | None
    busy: int
    requests: int
    errors: int
    tokens_in: int
    tokens_out: int
    last_used_at: datetime | None
    last_checked_at: datetime | None
    next_check_in: float | None
    created_at: datetime


def _out(k: ApiKey) -> KeyOut:
    return KeyOut(id=k.id, provider=k.provider, provider_name=PROVIDERS[k.provider].name if k.provider in PROVIDERS
                  else k.provider, label=k.label, tail=k.key_tail, base_url=k.base_url, status=k.status,
                  reason=k.status_reason, busy=key_pool.busy(k.id), requests=k.requests or 0, errors=k.errors or 0,
                  tokens_in=k.tokens_in or 0, tokens_out=k.tokens_out or 0, last_used_at=k.last_used_at,
                  last_checked_at=k.last_checked_at, next_check_in=next_check_in(k), created_at=k.created_at)


async def _own(db: AsyncSession, user: User, key_id: int) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    return key


@router.get("/providers")
async def providers() -> list[dict]:
    return public_list()


@router.get("")
async def list_keys(user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    keys = list((await db.execute(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id))).scalars())
    summary = {s: sum(1 for k in keys if k.status == s) for s in ("available", "limited", "invalid", "disabled")}
    summary["busy"] = sum(key_pool.busy(k.id) for k in keys)
    return {"keys": [_out(k).model_dump(mode="json") for k in keys], "summary": summary}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_key(body: KeyIn, user: Annotated[User, Depends(get_current_user)],
                  db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    try:
        provider = get_provider(body.provider)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown provider") from None
    secret = body.key.strip()
    base_url = (body.base_url or "").strip().rstrip("/") or None
    if provider.needs_base_url and not (base_url and base_url.startswith(("https://", "http://"))):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This provider needs a base URL (https://…/v1)")
    if not provider.needs_base_url:
        base_url = None
    # the same key twice is one key
    for other in (await db.execute(select(ApiKey).where(ApiKey.user_id == user.id,
                                                        ApiKey.provider == provider.id))).scalars():
        if other.key_tail == secret[-4:] and decrypt_str(other.key_encrypted) == secret:
            raise HTTPException(status.HTTP_409_CONFLICT, "This key is already in the pool")
    client = llm.make_client(provider, secret, base_url)
    outcome, code = llm.OK, None
    try:
        await client.probe(None)
    except llm.LLMError as exc:
        outcome, code = exc.outcome, exc.status
    finally:
        try:
            await client.close()
        except Exception:
            pass
    if outcome == llm.INVALID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"The provider did not accept this key ({code})")
    if outcome == llm.TRANSIENT or (outcome == llm.REQUEST and code != 404):  # 404: no /models on this service
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not check the key now ({code or 'no answer'}); try again")
    now = utcnow()
    key = ApiKey(user_id=user.id, provider=provider.id, label=body.label.strip() or f"{provider.name} …{secret[-4:]}",
                 key_encrypted=encrypt_str(secret), key_tail=secret[-4:], base_url=base_url,
                 status="available", last_checked_at=now)
    if outcome in (llm.RATE_LIMITED, llm.NO_CREDITS):
        from datetime import timedelta

        from app.agents.keypool import _setting

        key.status, key.limited_at = "limited", now
        key.status_reason = llm.short_reason(outcome, code)
        key.next_check_at = now + timedelta(seconds=float(_setting("KEYPOOL_CHECK_SECONDS", 300)))
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return _out(key).model_dump(mode="json")


@router.patch("/{key_id}")
async def patch_key(key_id: int, body: KeyPatch, user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    key = await _own(db, user, key_id)
    if body.label is not None:
        key.label = body.label.strip()
    if body.disabled is True:
        key.status, key.status_reason, key.next_check_at = "disabled", None, None
    elif body.disabled is False and key.status == "disabled":
        key.status, key.next_check_at = "limited", None  # re-checked right away by the checker
    await db.commit()
    await db.refresh(key)
    return _out(key).model_dump(mode="json")


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(key_id: int, user: Annotated[User, Depends(get_current_user)],
                     db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    key = await _own(db, user, key_id)
    await db.delete(key)
    await db.commit()


@router.post("/{key_id}/check")
async def check_key(key_id: int, user: Annotated[User, Depends(get_current_user)],
                    db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    key = await _own(db, user, key_id)
    outcome = await key_pool.check_key(key_id)
    await db.refresh(key)
    return {"outcome": outcome, "key": _out(key).model_dump(mode="json")}


_MODELS_CACHE: dict[tuple[int, str], tuple[float, list[str]]] = {}


@router.get("/models")
async def models(provider: Annotated[str, Query()], user: Annotated[User, Depends(get_current_user)],
                 db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    try:
        prov = get_provider(provider)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown provider") from None
    cached = _MODELS_CACHE.get((user.id, provider))
    if cached and time.monotonic() - cached[0] < 600:
        return {"provider": provider, "models": cached[1]}
    keys = list((await db.execute(select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.provider == provider,
                                                       ApiKey.status.in_(("available", "limited"))))).scalars())
    if not keys:
        return {"provider": provider, "models": [m for m in [prov.default_model] if m], "note": "no key"}
    last_error = None
    for key in sorted(keys, key=lambda k: k.status != "available"):
        client = llm.make_client(prov, decrypt_str(key.key_encrypted), key.base_url)
        try:
            names = await client.models()
            _MODELS_CACHE[(user.id, provider)] = (time.monotonic(), names)
            return {"provider": provider, "models": names}
        except llm.LLMError as exc:
            last_error = exc
        finally:
            try:
                await client.close()
            except Exception:
                pass
    raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                        f"Could not list models ({getattr(last_error, 'status', None) or 'no answer'})")
