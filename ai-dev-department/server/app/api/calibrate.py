"""Account self-calibration in the account's LIVE browser.

POST /api/accounts/{id}/calibrate -> start (202); GET -> {running, step, result}.
Runs in the background (a thinking model may take minutes). One calibration per
account at a time; different accounts can calibrate in parallel (own browsers).
The result holds booleans only (no page text); the learned selectors are stored
on the account and used by every later chat.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.api.accounts import _get_own_account
from app.models import Account

router = APIRouter(prefix="/api/accounts", tags=["calibrate"])
logger = logging.getLogger(__name__)

_runs: dict[int, dict] = {}
_tasks: set[asyncio.Task] = set()
_locks: dict[int, asyncio.Lock] = {}


class CalibrationStatus(BaseModel):
    running: bool
    step: str | None = None
    result: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def public_result(profile: dict | None) -> dict | None:
    """What the UI may show: booleans and timings, never selectors or page text."""
    if not profile:
        return None
    return {
        "ok": bool(profile.get("ok")),
        "reader": profile.get("reader"),
        "busy_found": bool(profile.get("busy")),
        "checks": profile.get("checks") or {},
        "reply_seconds": profile.get("reply_seconds"),
        "seconds": profile.get("seconds"),
        "modality": profile.get("modality"),
        "checked_at": profile.get("checked_at"),
        "reason": profile.get("reason"),
    }


async def run_calibration(account: Account, progress=None) -> dict:
    """Calibrate one account (used by the API and by the project preflight).
    Saves the profile on success. Returns the full profile."""
    from app.adapters.base import load_adapter
    from app.adapters.generic.calibrate import apply_profile, calibrate
    from app.db import SessionLocal
    from app.services.browser_manager import browser_manager
    from app.services.orchestrator import _ensure_context, _wire_auto_login

    lock = _locks.setdefault(account.id, asyncio.Lock())
    async with lock:
        context = await _ensure_context(account, session_factory=SessionLocal, browser_manager=browser_manager)
        if context is None:
            return {"ok": False, "reason": "no browser session for this account: open its browser and log in"}
        adapter = load_adapter()
        if not hasattr(adapter, "profile"):
            return {"ok": False, "reason": "the configured adapter does not support calibration"}
        _wire_auto_login(adapter, account, session_factory=SessionLocal, browser_manager=browser_manager)
        adapter.profile = None  # learn from scratch
        profile = await calibrate(adapter, context, progress=progress)
        if profile.get("ok"):
            await apply_profile(SessionLocal, account.id, profile)
        return profile


async def _run(account: Account) -> None:
    run = _runs[account.id]

    async def progress(text: str) -> None:
        run["step"] = text

    try:
        run["result"] = public_result(await run_calibration(account, progress))
    except Exception as exc:
        logger.warning("Calibration for account %s failed", account.id, exc_info=True)
        run["result"] = {"ok": False, "reason": f"{type(exc).__name__}"}
    finally:
        run["running"] = False
        run["step"] = None
        run["finished_at"] = _now()


@router.post("/{account_id}/calibrate", response_model=CalibrationStatus, status_code=status.HTTP_202_ACCEPTED)
async def start_calibration(account: Annotated[Account, Depends(_get_own_account)]) -> CalibrationStatus:
    current = _runs.get(account.id)
    if current and current["running"]:
        return CalibrationStatus(**current)
    _runs[account.id] = {"running": True, "step": "starting", "result": None, "started_at": _now(), "finished_at": None}
    task = asyncio.create_task(_run(account))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return CalibrationStatus(**_runs[account.id])


@router.get("/{account_id}/calibrate", response_model=CalibrationStatus)
async def calibration_status(account: Annotated[Account, Depends(_get_own_account)]) -> CalibrationStatus:
    current = _runs.get(account.id)
    if current is None:
        from app.adapters.generic.calibrate import profile_key
        from app.config import get_settings

        saved = (account.adapter_profile or {}).get(profile_key(get_settings()))
        return CalibrationStatus(running=False, result=public_result(saved))
    return CalibrationStatus(**current)
