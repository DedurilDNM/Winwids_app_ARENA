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


async def run_project(ctx, project_id: int, resume: bool = False) -> str:
    """arq entry point. `ctx` is the arq worker context (unused)."""
    logger.info("run_project %s for project %s", "resumed" if resume else "started", project_id)
    status = await run_project_logic(
        project_id,
        session_factory=SessionLocal,
        browser_manager=browser_manager,
        resume=resume,
    )
    logger.info("run_project finished for project %s: %s", project_id, status)
    return status


_inline_tasks: set[asyncio.Task] = set()  # strong refs: unreferenced tasks can be GC'd


def _run_inline(project_id: int, resume: bool = False) -> None:
    task = asyncio.create_task(run_project({}, project_id, resume))
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)


async def enqueue_run_project(project_id: int, resume: bool = False) -> str:
    """Run in the API process (default) or enqueue via arq. Returns 'inline' or 'arq'."""
    settings = get_settings()
    if settings.PROJECT_RUNNER != "arq":
        _run_inline(project_id, resume)
        return "inline"
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        try:
            await pool.enqueue_job("run_project", project_id, resume)
        finally:
            await pool.close()
        return "arq"
    except Exception:
        logger.warning(
            "Redis/arq unavailable (%s); running project %s inline in the API process",
            settings.REDIS_URL, project_id, exc_info=True,
        )
        _run_inline(project_id, resume)
        return "inline"
