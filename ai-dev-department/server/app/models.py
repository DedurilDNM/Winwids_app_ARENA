"""SQLAlchemy 2.0 models.

Timezone strategy: naive UTC datetimes (`datetime.now(timezone.utc).replace(tzinfo=None)`)
so the same code works unchanged on SQLite (naive) and PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Role catalogue (validated in pydantic schemas as well) ---
ACCOUNT_ROLES = ("Architect", "Coder", "Reviewer", "Tester", "Integrator", "Orchestrator")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Account(Base):
    """An account on the external AI service.

    We never store the account's password. Only the encrypted (Fernet)
    `storage_state` blob (cookies + localStorage + IndexedDB) captured after
    a manual VNC login.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    service_note: Mapped[str] = mapped_column(String(255), default="")
    default_role: Mapped[str] = mapped_column(String(32), default="Coder")
    github_repos: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending_login", index=True)
    # Fernet-encrypted Playwright/Camoufox storage_state (bytes). Never logged.
    storage_state_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    task: Mapped[str] = mapped_column(Text)
    github_repo: Mapped[str] = mapped_column(String(300))
    orchestrator_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProjectAssignment(Base):
    """Many-to-many: which account plays which role in a project."""

    __tablename__ = "project_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    task: Mapped[str] = mapped_column(Text)
    assigned_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ConnectorCredential(Base):
    """Future-proofing: Fernet-encrypted credentials for connectors (GitHub tokens etc.).

    The service's own GitHub connector is used by the AI agents themselves;
    these tokens belong to the user and are only ever used by the server
    (e.g. github_watcher). Stored encrypted, never in plain text, never logged.
    """

    __tablename__ = "connector_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # "github" for now
    label: Mapped[str] = mapped_column(String(120), default="")
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def account_to_summary(account: Account) -> dict[str, Any]:
    return {
        "id": account.id,
        "name": account.name,
        "default_role": account.default_role,
        "github_repos": account.github_repos,
        "status": account.status,
    }
