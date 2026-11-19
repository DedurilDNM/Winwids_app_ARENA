"""arq task `run_project` + enqueue helper.

POST /api/projects/{id}/start enqueues this job. If Redis is unavailable
(dev/sandbox without a broker), the job runs inline as an asyncio task in
the API process instead - the project still runs, just without worker
isolation. The fallback is logged loudly.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.db import SessionLocal
from app.services.browser_manager import browser_manager
from app.services.orchestrator import run_project_logic

logger = logging.getLogger(__name__)


async def run_project(ctx, project_id: int) -> str:
    """arq entry point. `ctx` is the arq worker context (unused)."""
    logger.info("run_project started for project %s", project_id)
    status = await run_project_logic(
        project_id,
        session_factory=SessionLocal,
        browser_manager=browser_manager,
    )
    logger.info("run_project finished for project %s: %s", project_id, status)
    return status


async def enqueue_run_project(project_id: int) -> str:
    """Enqueue via arq; fall back to an inline asyncio task. Returns 'arq' or 'inline'."""
    settings = get_settings()
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        try:
            await pool.enqueue_job("run_project", project_id)
        finally:
            await pool.close()
        return "arq"
    except Exception:
        logger.warning(
            "Redis/arq unavailable (%s); running project %s inline in the API process",
            settings.REDIS_URL, project_id, exc_info=True,
        )
        asyncio.create_task(run_project({}, project_id))
        return "inline"
