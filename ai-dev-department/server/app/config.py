"""Application configuration.

Reads settings from `.env` (next to this package's parent directory, i.e.
`server/.env`) or from real environment variables. On first run the
`FERNET_KEY` and `JWT_SECRET` values are auto-generated and persisted into
the `.env` file (chmod 600). No concrete external AI service is referenced
anywhere in the codebase - everything goes through `SERVICE_URL` /
`SERVICE_DISPLAY_NAME` / `ADAPTER_MODULE`.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = SERVER_ROOT / ".env"
DATA_DIR = SERVER_ROOT / "data"


def _ensure_env_secrets() -> None:
    """Generate FERNET_KEY / JWT_SECRET on first run and persist them into .env.

    Existing values (in .env or real environment) are never overwritten.
    Empty placeholder lines like `FERNET_KEY=` are replaced in place, so the
    file never ends up with duplicate keys.
    """
    from cryptography.fernet import Fernet

    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    def _entry_value(line: str) -> tuple[str, str] | None:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            return None
        key, _, value = stripped.partition("=")
        return key.strip(), value.strip().strip('"').strip("'")

    existing: dict[str, str] = {}
    for line in lines:
        entry = _entry_value(line)
        if entry:
            existing[entry[0]] = entry[1]  # last wins, mirrors dotenv parsing

    missing = {
        "FERNET_KEY": Fernet.generate_key().decode(),
        "JWT_SECRET": secrets.token_urlsafe(48),
    }
    to_set = {
        key: value
        for key, value in missing.items()
        if not existing.get(key) and not os.environ.get(key)
    }
    if not to_set:
        try:
            if ENV_FILE.exists():
                ENV_FILE.chmod(0o600)
        except OSError:
            pass
        return

    new_lines: list[str] = []
    written: set[str] = set()
    for line in lines:
        entry = _entry_value(line)
        if entry and entry[0] in to_set and not entry[1]:
            # Replace the empty placeholder in place.
            new_lines.append(f"{entry[0]}={to_set[entry[0]]}")
            written.add(entry[0])
        else:
            new_lines.append(line)
    for key, value in to_set.items():
        if key not in written:
            new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        # Best effort on filesystems that do not support chmod (e.g. some dev mounts)
        pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- External AI service (never name a concrete service here) ---
    SERVICE_URL: str = "https://example.com"
    SERVICE_DISPLAY_NAME: str = "External AI Service"
    ADAPTER_MODULE: str = "app.adapters.generic.adapter"
    PROXY_URL: str = ""  # e.g. http://user:pass@host:port ; all service traffic goes through it

    # --- Secrets (auto-generated on first run) ---
    FERNET_KEY: str = ""
    JWT_SECRET: str = ""
    ADMIN_PASSWORD: str = ""  # used only when the very first admin is created

    # --- Infrastructure ---
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DATA_DIR / 'app.db'}"
    REDIS_URL: str = "redis://localhost:6379/0"
    GITHUB_API_URL: str = "https://api.github.com"

    # --- Tokens ---
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # --- HTTP / CORS ---
    # Comma-separated list. Dev default: localhost only. Prod: set to the client domain.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173"

    ENVIRONMENT: str = "development"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _default_database_url(cls, value: object) -> object:
        # Allow an empty DATABASE_URL in .env to mean "use the default".
        if value is None or (isinstance(value, str) and not value.strip()):
            return f"sqlite+aiosqlite:///{DATA_DIR / 'app.db'}"
        return value

    @field_validator("GITHUB_API_URL", mode="before")
    @classmethod
    def _default_github_api_url(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "https://api.github.com"
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    _ensure_env_secrets()
    return Settings()
