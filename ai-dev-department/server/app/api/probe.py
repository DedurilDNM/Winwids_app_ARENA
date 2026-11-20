"""In-app adapter diagnostics: run app.tools.adapter_probe inside the API
process, in the account's LIVE browser (the one agents use).

POST /api/accounts/{id}/probe -> start (202); GET -> {running, report}.
The probe runs in the background (it takes 1-4 minutes and tunnels cut long
requests). One probe at a time per server. The report never contains page
URLs, titles or page text - only element structure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.accounts import _get_own_account
from app.models import Account

router = APIRouter(prefix="/api/accounts", tags=["probe"])
logger = logging.getLogger(__name__)

_runs: dict[int, dict] = {}
_tasks: set[asyncio.Task] = set()
_lock = asyncio.Lock()


class ProbeStatus(BaseModel):
    running: bool
    report: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _context_for(account: Account):
    from app.config import get_settings
    from app.security import decrypt_bytes
    from app.services.browser_manager import browser_manager
    from app.services.vnc_manager import vnc_manager

    context = browser_manager.get_context(account.id)
    if context is not None:
        return context
    if not account.storage_state_encrypted:
        return None
    display = None if get_settings().VNC_HEADLESS else await vnc_manager.ensure_shared_display()
    return await browser_manager.launch(
        account.id,
        storage_state=json.loads(decrypt_bytes(account.storage_state_encrypted)),
        display=display,
        headless=display is None,
        open_url=False,
    )


async def _run(account_id: int, snapshot: Account) -> None:
    from app.tools.adapter_probe import run_probe_in_context

    run = _runs[account_id]
    try:
        async with _lock:
            context = await _context_for(snapshot)
            if context is None:
                run["report"] = "No browser for this account: open it («Открыть браузер»), log in and save the session."
            else:
                run["report"] = await run_probe_in_context(context, snapshot)
    except Exception as exc:
        logger.warning("Probe for account %s failed", account_id, exc_info=True)
        run["report"] = f"probe failed: {type(exc).__name__}"
    finally:
        run["running"] = False
        run["finished_at"] = _now()


@router.post("/{account_id}/probe", response_model=ProbeStatus, status_code=status.HTTP_202_ACCEPTED)
async def start_probe(account: Annotated[Account, Depends(_get_own_account)]) -> ProbeStatus:
    current = _runs.get(account.id)
    if current and current["running"]:
        return ProbeStatus(**current)
    if _lock.locked():
        raise HTTPException(status_code=409, detail="Another diagnostic is running, try again in a few minutes")
    _runs[account.id] = {"running": True, "report": None, "started_at": _now(), "finished_at": None}
    task = asyncio.create_task(_run(account.id, account))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return ProbeStatus(**_runs[account.id])


@router.get("/{account_id}/probe", response_model=ProbeStatus)
async def probe_status(account: Annotated[Account, Depends(_get_own_account)]) -> ProbeStatus:
    current = _runs.get(account.id)
    if current is None:
        return ProbeStatus(running=False)
    return ProbeStatus(**current)
