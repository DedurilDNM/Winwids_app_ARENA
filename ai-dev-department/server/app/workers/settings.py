"""arq WorkerSettings.

Run:  python -m arq app.workers.settings.WorkerSettings

- functions: run_project (enqueued by POST /api/projects/{id}/start)
- cron: session_health_check every 5 minutes (is_alive per active account)
"""

from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.workers.run_project import run_project
from app.workers.session_check import session_health_check

_EVERY_5_MINUTES = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

WorkerSettings = {
    "functions": [
        run_project,
        cron(session_health_check, minute=_EVERY_5_MINUTES, run_at_startup=True),
    ],
    "redis_settings": RedisSettings.from_dsn(get_settings().REDIS_URL),
    # A project can legitimately run for hours (agent tasks + polling).
    "job_timeout": 6 * 3600,
    "max_tries": 1,  # run_project_logic marks the project failed itself; no silent retries
    "max_jobs": 10,
    "keep_result": 3600,
}
