"""HTTP API client for the AI Dev Department server.

Thin wrapper around httpx that:
- attaches the Bearer access token from AuthStore,
- refreshes the token automatically on 401 (using the refresh token),
- raises ApiError with the server's detail message on non-2xx responses.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.auth_store import AuthStore


class ApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


class ApiClient:
    def __init__(self, base_url: str, auth: AuthStore) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    # ------------------------------------------------------------------ auth flow

    def _auth_headers(self) -> dict[str, str]:
        token = self.auth.access_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        response = self._client.request(
            method, url, json=json, params=params, headers=self._auth_headers()
        )
        if response.status_code == 401:
            # Try once to refresh the access token.
            if self.auth.refresh():
                response = self._client.request(
                    method, url, json=json, params=params, headers=self._auth_headers()
                )
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            detail = None
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text[:200] or None
            raise ApiError(response.status_code, detail)

    # ---------------------------------------------------------------- endpoints

    def login(self, email: str, password: str) -> dict[str, str]:
        response = self._request(
            "POST", "/api/auth/login", json={"email": email, "password": password}
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
        )
        self._raise_for_status(response)
        data = response.json()
        self.auth.save_tokens(data["access_token"], data["refresh_token"])
        return data

    def refresh(self) -> bool:
        return self.auth.refresh()

    # ---------------------------------------------------------------- accounts

    def list_accounts(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/accounts")
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

    def vnc_stop(self, account_id: int) -> dict[str, Any]:
        response = self._request("POST", f"/api/accounts/{account_id}/vnc/stop")
        self._raise_for_status(response)
        return response.json()

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
