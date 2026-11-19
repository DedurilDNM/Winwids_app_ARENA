"""GitHub connector: raw GitHub REST API over httpx (no heavy SDK).

Used by the server-side github_watcher to poll `/status/task-<id>.md` files
on task branches. The token (if any) comes from the user's encrypted
ConnectorCredential or the GITHUB_TOKEN setting - it is never logged.

Note: this is NOT service traffic, so it does not go through PROXY_URL by
default (PROXY_URL is for the external AI service). Set GITHUB_PROXY_URL if
your network requires a proxy for api.github.com as well.
"""

from __future__ import annotations

import base64
import binascii
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class GitHubConnector:
    kind = "github"

    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str | None = None,
        proxy: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self._api_url = (api_url or settings.GITHUB_API_URL).rstrip("/")
        self._token = token or settings.GITHUB_TOKEN or None
        self._proxy = proxy if proxy is not None else (settings.GITHUB_PROXY_URL or None)
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-dev-department-server",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get_json(self, url: str, params: dict | None = None):
        async with httpx.AsyncClient(
            base_url=self._api_url,
            headers=self._headers(),
            proxy=self._proxy,
            timeout=self._timeout,
        ) as client:
            response = await client.get(url, params=params)
        if response.status_code == 404:
            return None
        if response.status_code == 403 and "rate limit" in response.text.lower():
            logger.warning("GitHub API rate limit hit (url=%s)", url)
            return None
        response.raise_for_status()
        return response.json()

    async def read_file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        params = {"ref": ref} if ref else None
        data = await self._get_json(f"/repos/{repo}/contents/{path}", params=params)
        if not isinstance(data, dict):
            return None
        content = data.get("content")
        encoding = data.get("encoding")
        if not content or encoding != "base64":
            return None
        try:
            return base64.b64decode(content.replace("\n", "")).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            logger.warning("Could not base64-decode %s:%s", repo, path)
            return None

    async def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[str]:
        params = {"ref": ref} if ref else None
        data = await self._get_json(f"/repos/{repo}/contents/{path}", params=params)
        if not isinstance(data, list):
            return []
        return [entry.get("name", "") for entry in data if isinstance(entry, dict) and entry.get("name")]
