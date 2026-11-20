"""Token storage for the PySide6 client.

Tokens are kept in memory only (QSettings would persist them in plain text,
which is not acceptable for JWT access/refresh tokens). On logout they are
wiped.
"""

from __future__ import annotations


class AuthStore:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    def save_tokens(self, access_token: str, refresh_token: str) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token

    def clear(self) -> None:
        self._access_token = None
        self._refresh_token = None

    def refresh(self) -> bool:
        """Kept for backwards compatibility: the actual HTTP refresh
        (POST /api/auth/refresh) lives in ApiClient._try_refresh(), because
        AuthStore has no HTTP client. Returns whether a refresh is possible."""
        return bool(self._refresh_token)
