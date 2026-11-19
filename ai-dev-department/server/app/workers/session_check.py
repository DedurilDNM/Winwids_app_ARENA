"""Session health cron: adapter.is_alive per active account every 5 minutes.

Dead sessions flip the account to `relogin_needed` and emit WS `session_lost`
to affected projects; recovered sessions emit `session_relogged`.
"""

from __future__ import annotations

import logging

from app.db import SessionLocal
from app.services.browser_manager import browser_manager
from app.services.orchestrator import check_all_sessions

logger = logging.getLogger(__name__)


async def session_health_check(ctx) -> dict[str, int]:
    """arq cron entry point."""
    return await check_all_sessions(
        session_factory=SessionLocal,
        browser_manager=browser_manager,
    )
