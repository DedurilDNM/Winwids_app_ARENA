"""GitHub watcher: polls /status/task-<id>.md on task branches.

The agents signal completion by committing a status file on their branch
(`status/task-<id>.md`, first line DONE or BLOCKED). The server polls the
GitHub API through a Connector (see app/connectors) - the agents themselves
never talk to our server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.connectors.base import Connector
from app.connectors.github import GitHubConnector
from app.config import get_settings

logger = logging.getLogger(__name__)


def parse_status_content(content: str | None) -> str | None:
    """Return 'done' / 'blocked' from a status file body, or None if not final yet."""
    if not content:
        return None
    for line in content.splitlines():
        stripped = line.strip().upper()
        if not stripped:
            continue
        if stripped.startswith("DONE"):
            return "done"
        if stripped.startswith("BLOCKED"):
            return "blocked"
        return None
    return None


async def load_github_token(session_factory, user_id: int) -> str | None:
    """Resolve the GitHub token for a user: encrypted credential first, then env.

    ConnectorCredential payloads are Fernet-encrypted JSON ({"token": "..."})
    or a raw token string. Never logged.
    """
    from sqlalchemy import select

    from app.models import ConnectorCredential
    from app.security import decrypt_bytes

    async with session_factory() as session:
        row = (
            await session.scalars(
                select(ConnectorCredential)
                .where(
                    ConnectorCredential.user_id == user_id,
                    ConnectorCredential.kind == "github",
                )
                .order_by(ConnectorCredential.id.desc())
            )
        ).first()
    if row is not None:
        try:
            raw = decrypt_bytes(row.encrypted_payload).decode("utf-8", errors="replace").strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    token = parsed.get("token") or parsed.get("access_token")
                    if token:
                        return str(token)
            except (ValueError, TypeError):
                pass
            if raw:
                return raw
        except Exception:
            logger.warning("Could not decrypt GitHub credential for user %s", user_id)
    settings = get_settings()
    return settings.GITHUB_TOKEN or None


class GitHubWatcher:
    """Polls one task's status file until DONE / BLOCKED / timeout."""

    def __init__(self, connector: Connector | None = None) -> None:
        self.connector: Connector = connector or GitHubConnector()

    async def wait_for_task(
        self,
        *,
        repo: str,
        task_id: int,
        branch: str,
        timeout: float,
        poll_interval: float,
        should_continue=None,
    ) -> str:
        """Return 'done', 'blocked' or 'timeout'.

        `should_continue` is an optional async callable returning False when
        the wait must abort early (e.g. the project was stopped by the user);
        in that case 'timeout' is returned and the caller checks project state.
        """
        path = f"status/task-{task_id}.md"
        deadline = time.monotonic() + timeout
        while True:
            if should_continue is not None and not await should_continue():
                return "timeout"
            try:
                content = await self.connector.read_file(repo, path, ref=branch)
            except Exception:
                logger.warning(
                    "GitHub poll failed for %s:%s (branch %s); will retry",
                    repo, path, branch, exc_info=True,
                )
                content = None
            status = parse_status_content(content)
            if status is not None:
                return status
            if time.monotonic() >= deadline:
                return "timeout"
            await asyncio.sleep(poll_interval)


def build_watcher(connector: Connector | None = None) -> GitHubWatcher:
    return GitHubWatcher(connector)


async def build_watcher_for_user(session_factory, user_id: int) -> GitHubWatcher:
    token = await load_github_token(session_factory, user_id)
    return GitHubWatcher(GitHubConnector(token=token))


# Re-exported for tests / type checkers that import from here.
__all__ = [
    "GitHubWatcher",
    "build_watcher",
    "build_watcher_for_user",
    "load_github_token",
    "parse_status_content",
]
