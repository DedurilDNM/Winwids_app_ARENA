"""GitHub connector: raw GitHub REST API over httpx (no heavy SDK).

Read access (files, trees) and write access (branches, multi-file commits,
pull requests). Agents never touch GitHub themselves: they return files in
their chat replies and the SERVER commits them with the user's token. The
token comes from the user's encrypted ConnectorCredential or the
GITHUB_TOKEN setting - it is never logged.

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


class GitHubError(RuntimeError):
    """A GitHub API call failed. `message` is safe to show to the user."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error_message(response: httpx.Response) -> str:
    try:
        detail = response.json().get("message") or ""
    except Exception:
        detail = response.text[:200]
    if response.status_code == 401:
        return "GitHub rejected the token (expired or revoked)"
    if response.status_code == 403 and "rate limit" in detail.lower():
        return "GitHub API rate limit exceeded, try again later"
    if response.status_code == 403:
        return f"The token has no permission for this action ({detail})"
    if response.status_code == 404:
        return "Repository not found or the token has no access to it"
    return f"GitHub API error {response.status_code}: {detail}"


class GitHubConnector:
    kind = "github"

    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str | None = None,
        proxy: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self._api_url = (api_url or settings.GITHUB_API_URL).rstrip("/")
        self._token = token or settings.GITHUB_TOKEN or None
        self._proxy = proxy if proxy is not None else (settings.GITHUB_PROXY_URL or None)
        self._timeout = timeout
        self._transport = transport  # tests inject an in-memory GitHub

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-dev-department-server",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        kwargs = {"base_url": self._api_url, "headers": self._headers(), "timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            kwargs["proxy"] = self._proxy
        return httpx.AsyncClient(**kwargs)

    async def _call(self, method: str, url: str, *, json=None, params=None, ok=(200, 201)) -> httpx.Response:
        """Write-path request: raises GitHubError with a user-safe message."""
        try:
            async with self._client() as client:
                response = await client.request(method, url, json=json, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(0, f"Cannot reach GitHub: {type(exc).__name__}") from exc
        if response.status_code not in ok:
            raise GitHubError(response.status_code, _error_message(response))
        return response

    async def _get_json(self, url: str, params: dict | None = None):
        async with self._client() as client:
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

    # ------------------------------------------------------------ write path

    async def get_user(self) -> dict:
        """The token owner (validates the token)."""
        return (await self._call("GET", "/user")).json()

    async def get_repo(self, repo: str) -> dict:
        """Repository metadata incl. default_branch and permissions.push."""
        return (await self._call("GET", f"/repos/{repo}")).json()

    async def get_branch_sha(self, repo: str, branch: str) -> str | None:
        try:
            data = (await self._call("GET", f"/repos/{repo}/git/ref/heads/{branch}")).json()
        except GitHubError as exc:
            if exc.status in (404, 409):  # missing branch / empty repository
                return None
            raise
        return data["object"]["sha"]

    async def ensure_branch(self, repo: str, branch: str, from_branch: str) -> str:
        """Create `branch` from `from_branch` unless it exists. Returns its head sha."""
        sha = await self.get_branch_sha(repo, branch)
        if sha:
            return sha
        base = await self.get_branch_sha(repo, from_branch)
        if not base:
            raise GitHubError(409, f"Branch '{from_branch}' not found - is the repository empty? Add a README first.")
        await self._call("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base})
        return base

    async def list_tree(self, repo: str, ref: str) -> list[str]:
        """All file paths at `ref` (recursive)."""
        sha = await self.get_branch_sha(repo, ref)
        if not sha:
            return []
        data = (await self._call("GET", f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})).json()
        return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]

    async def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        """Atomically commit `files` (path -> text) on top of `branch`. Returns the commit sha."""
        head = await self.get_branch_sha(repo, branch)
        if not head:
            raise GitHubError(404, f"Branch '{branch}' does not exist")
        base_tree = (await self._call("GET", f"/repos/{repo}/git/commits/{head}")).json()["tree"]["sha"]
        tree = (await self._call("POST", f"/repos/{repo}/git/trees", json={
            "base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                     for path, content in files.items()],
        })).json()["sha"]
        commit = (await self._call("POST", f"/repos/{repo}/git/commits", json={
            "message": message, "tree": tree, "parents": [head],
        })).json()["sha"]
        await self._call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit})
        return commit

    async def create_pull_request(self, repo: str, *, head: str, base: str, title: str, body: str) -> str:
        """Open a PR (or return the already open one). Returns its html_url."""
        try:
            pr = (await self._call("POST", f"/repos/{repo}/pulls", json={
                "title": title, "head": head, "base": base, "body": body,
            })).json()
            return pr["html_url"]
        except GitHubError as exc:
            if exc.status != 422:
                raise
        owner = repo.split("/", 1)[0]
        existing = (await self._call("GET", f"/repos/{repo}/pulls",
                                     params={"head": f"{owner}:{head}", "state": "open"})).json()
        if existing:
            return existing[0]["html_url"]
        raise GitHubError(422, "Could not open a pull request (no changes compared to the base branch?)")
