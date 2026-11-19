"""VNC session lifecycle API.

POST /api/accounts/{id}/vnc/start   -> start (or reuse) a VNC session, returns vnc_url
POST /api/accounts/{id}/vnc/stop    -> stop the noVNC path (browser stays alive)
GET  /api/accounts/{id}/vnc         -> introspection of the current session

The noVNC URL points at the local websockify instance (mapped to a public
path by cloudflared/nginx in production). The token is a short-lived JWT.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.models import Account
from app.schemas import VncInfoResponse, VncStartResponse
from app.services.browser_manager import BrowserUnavailableError
from app.services.vnc_manager import VncUnavailableError, vnc_manager
from app.api.accounts import _get_own_account

router = APIRouter(prefix="/api/accounts", tags=["vnc"])


@router.post("/{account_id}/vnc/start", response_model=VncStartResponse)
async def start_vnc(
    account: Annotated[Account, Depends(_get_own_account)],
) -> VncStartResponse:
    try:
        session = await vnc_manager.start_session(account)
    except (VncUnavailableError, BrowserUnavailableError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    if session.headless:
        return VncStartResponse(live=True, headless=True, vnc_url=None)
    return VncStartResponse(live=True, headless=False, vnc_url=vnc_manager.vnc_url_for(session))


@router.post("/{account_id}/vnc/stop", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def stop_vnc(
    account: Annotated[Account, Depends(_get_own_account)],
) -> None:
    await vnc_manager.stop_session(account.id, keep_browser=True)


@router.get("/{account_id}/vnc", response_model=VncInfoResponse)
async def vnc_info(
    account: Annotated[Account, Depends(_get_own_account)],
) -> VncInfoResponse:
    session = vnc_manager.get_session(account.id)
    if session is None:
        return VncInfoResponse(live=False)
    vnc_url = None if session.headless else vnc_manager.vnc_url_for(session)
    return VncInfoResponse(live=True, headless=session.headless, vnc_url=vnc_url)
