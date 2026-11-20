"""HTTP API client for the AI Dev Department server.

Thin wrapper around httpx that:
- attaches the Bearer access token from AuthStore,
- refreshes the token automatically on 401 (POST /api/auth/refresh),
- raises ApiError with the server's detail message on non-2xx responses,
- converts network failures (server down, timeout, DNS) into ApiError with
  status_code=0, so UI code only has to catch one exception type.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx

from app.auth_store import AuthStore


class ApiError(RuntimeError):
    """Any failure talking to the server.

    status_code == 0 means a network-level error (no HTTP response).
    """

    def __init__(self, status_code: int, detail: str | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")

    @property
    def message(self) -> str:
        """Human-readable text for message boxes."""
        if self.status_code == 0:
            return f"Нет связи с сервером: {self.detail}"
        return str(self.detail or f"HTTP {self.status_code}")


def _format_detail(detail: Any) -> str | None:
    """FastAPI returns `detail` either as a string or as a list of
    validation errors ({loc, msg, ...}); flatten both into one line."""
    if detail is None:
        return None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else msg)
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)


class ApiClient:
    def __init__(self, base_url: str, auth: AuthStore) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------------ auth flow

    def _auth_headers(self) -> dict[str, str]:
        token = self.auth.access_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _send(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method, url, json=json, params=params, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ApiError(0, f"таймаут запроса ({exc.__class__.__name__})") from exc
        except httpx.RequestError as exc:
            raise ApiError(0, str(exc) or exc.__class__.__name__) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = self._auth_headers() if auth else {}
        response = self._send(method, url, json=json, params=params, headers=headers)
        if auth and response.status_code == 401 and self._try_refresh():
            # Access token expired: refreshed successfully, retry once.
            response = self._send(
                method, url, json=json, params=params, headers=self._auth_headers()
            )
        return response

    def _try_refresh(self) -> bool:
        """Exchange the refresh token for a new pair. Thread-safe: concurrent
        401s from worker threads trigger a single refresh."""
        refresh_token = self.auth.refresh_token
        if not refresh_token:
            return False
        with self._refresh_lock:
            if self.auth.refresh_token != refresh_token:
                # Another thread already refreshed while we were waiting.
                return self.auth.is_authenticated
            try:
                response = self._send(
                    "POST",
                    f"{self.base_url}/api/auth/refresh",
                    json={"refresh_token": refresh_token},
                )
            except ApiError:
                return False
            if response.status_code != 200:
                self.auth.clear()
                return False
            data = response.json()
            self.auth.save_tokens(data["access_token"], data["refresh_token"])
            return True

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            detail: str | None
            try:
                body = response.json()
                detail = _format_detail(body.get("detail") if isinstance(body, dict) else body)
            except Exception:
                detail = response.text[:200] or None
            raise ApiError(response.status_code, detail)

    # ---------------------------------------------------------------- endpoints

    def login(self, email: str, password: str) -> dict[str, str]:
        response = self._request(
            "POST",
            "/api/auth/login",
            json={"email": email, "password": password},
            auth=False,
        )
        self._raise_for_status(response)
        data = response.json()
        self.auth.save_tokens(data["access_token"], data["refresh_token"])
        return data

    def register(
        self, email: str, password: str, invite_code: str
    ) -> dict[str, str]:
        response = self._request(
            "POST",
            "/api/auth/register",
            json={"email": email, "password": password, "invite_code": invite_code},
            auth=False,
        )
        self._raise_for_status(response)
        data = response.json()
        self.auth.save_tokens(data["access_token"], data["refresh_token"])
        return data

    def refresh(self) -> bool:
        return self._try_refresh()

    # ---------------------------------------------------------------- accounts

    def list_accounts(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/accounts")
        self._raise_for_status(response)
        return response.json()

    def get_account(self, account_id: int) -> dict[str, Any]:
        response = self._request("GET", f"/api/accounts/{account_id}")
        self._raise_for_status(response)
        return response.json()

    def create_account(
        self,
        *,
        name: str,
        default_role: str,
        github_repos: list[str] | None = None,
        service_note: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/accounts",
            json={
                "name": name,
                "default_role": default_role,
                "github_repos": github_repos or [],
                "service_note": service_note,
            },
        )
        self._raise_for_status(response)
        return response.json()

    def update_account(
        self, account_id: int, **fields: Any
    ) -> dict[str, Any]:
        response = self._request("PATCH", f"/api/accounts/{account_id}", json=fields)
        self._raise_for_status(response)
        return response.json()

    def delete_account(self, account_id: int) -> None:
        response = self._request("DELETE", f"/api/accounts/{account_id}")
        self._raise_for_status(response)

    def confirm_login(self, account_id: int) -> dict[str, Any]:
        response = self._request("POST", f"/api/accounts/{account_id}/confirm-login")
        self._raise_for_status(response)
        return response.json()

    def vnc_start(self, account_id: int) -> dict[str, Any]:
        response = self._request("POST", f"/api/accounts/{account_id}/vnc/start")
        self._raise_for_status(response)
        return response.json()

    def vnc_stop(self, account_id: int) -> None:
        # Server answers 204 No Content (browser stays alive, noVNC path closed).
        response = self._request("POST", f"/api/accounts/{account_id}/vnc/stop")
        self._raise_for_status(response)

    def vnc_info(self, account_id: int) -> dict[str, Any]:
        response = self._request("GET", f"/api/accounts/{account_id}/vnc")
        self._raise_for_status(response)
        return response.json()

    # ---------------------------------------------------------------- projects

    def list_projects(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/projects")
        self._raise_for_status(response)
        return response.json()

    def get_project(self, project_id: int) -> dict[str, Any]:
        response = self._request("GET", f"/api/projects/{project_id}")
        self._raise_for_status(response)
        return response.json()

    def create_project(
        self,
        *,
        name: str,
        description: str,
        task: str,
        github_repo: str,
        orchestrator_account_id: int | None = None,
        assignments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/projects",
            json={
                "name": name,
                "description": description,
                "task": task,
                "github_repo": github_repo,
                "orchestrator_account_id": orchestrator_account_id,
                "assignments": assignments or [],
            },
        )
        self._raise_for_status(response)
        return response.json()

    def update_project(
        self, project_id: int, **fields: Any
    ) -> dict[str, Any]:
        response = self._request("PATCH", f"/api/projects/{project_id}", json=fields)
        self._raise_for_status(response)
        return response.json()

    def delete_project(self, project_id: int) -> None:
        response = self._request("DELETE", f"/api/projects/{project_id}")
        self._raise_for_status(response)

    def start_project(self, project_id: int) -> dict[str, Any]:
        response = self._request("POST", f"/api/projects/{project_id}/start")
        self._raise_for_status(response)
        return response.json()

    def stop_project(self, project_id: int) -> dict[str, Any]:
        response = self._request("POST", f"/api/projects/{project_id}/stop")
        self._raise_for_status(response)
        return response.json()

    def close(self) -> None:
        self._client.close()
