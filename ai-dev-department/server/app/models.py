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

    The session is the encrypted (Fernet) `storage_state` blob (cookies +
    localStorage + IndexedDB) captured after a login. Optional login
    credentials (email + Fernet-encrypted password) let the server log in by
    itself when the service logs the account out. Never logged or returned.
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
    # Auto-login (optional). Empty email -> the first e-mail address in service_note.
    login_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    login_password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # The service model this account's chat must use (checked before each task). Empty = any.
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # What calibration learned about the service page, per chat modality:
    # {"Text": {"ok": true, "reader": "selector", "message": "...", "busy": "...", ...}}
    adapter_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    @property
    def has_login_password(self) -> bool:
        return bool(self.login_password_encrypted)

    @property
    def calibration(self) -> dict | None:
        """The saved calibration for the current chat modality (booleans only)."""
        from app.adapters.generic.calibrate import profile_key
        from app.api.calibrate import public_result
        from app.config import get_settings

        return public_result((self.adapter_profile or {}).get(profile_key(get_settings())))


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
    work_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)  # server commits here
    max_rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # review rounds (default 3)
    ci_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # GitHub Actions loop (default on)
    ci_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ci_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
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
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # agent's SUMMARY line
    # «Big department»: plan-local key ("t1"), task ids it waits for, the paths
    # it may write (its area), and the review round (0 = initial plan).
    plan_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)
    scope: Mapped[list | None] = mapped_column(JSON, nullable=True)
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files: Mapped[list | None] = mapped_column(JSON, nullable=True)  # paths the task committed
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)  # automatic retries used
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)  # server-made commit
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


class AgentMessage(Base):
    """A message one agent left for others (or a question to the orchestrator).
    Delivered in the recipient's next prompt; shown in the project feed."""

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    from_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_name: Mapped[str] = mapped_column(String(120), default="")
    to: Mapped[str] = mapped_column(String(120), default="all")  # role | account name | "all" | "orchestrator"
    kind: Mapped[str] = mapped_column(String(16), default="message")  # message | question | answer
    text: Mapped[str] = mapped_column(Text)
    delivered_to: Mapped[list] = mapped_column(JSON, default=list)  # account ids that got it
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- API department: key pool, role configs, runs -------------------------------

API_KEY_STATUSES = ("available", "limited", "invalid", "disabled")


class ApiKey(Base):
    """A provider API key in the user's common pool («котёл»). The secret is
    Fernet-encrypted and never returned or logged; only `key_tail` is shown."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    key_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    key_tail: Mapped[str] = mapped_column(String(8), default="")
    base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)  # «custom» provider only
    status: Mapped[str] = mapped_column(String(16), default="available", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)  # no secrets, no page text
    limited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRole(Base):
    """How one role of the API department works: provider, model, tools, skills."""

    __tablename__ = "agent_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tools: Mapped[list | None] = mapped_column(JSON, nullable=True)  # None = the role's defaults
    tools_seen: Mapped[list | None] = mapped_column(JSON, nullable=True)  # the tools there were when saved
    skills: Mapped[list | None] = mapped_column(JSON, nullable=True)  # None = every skill
    instructions: Mapped[str] = mapped_column(Text, default="")
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # [{"provider", "model"}]: used in order when every key of the main provider is limited
    fallbacks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    auto: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # chosen by the model picker
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentRun(Base):
    """One job for the API department: a lead agent and its sub-agents in a workspace."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    prompt: Mapped[str] = mapped_column(Text)
    lead_role: Mapped[str] = mapped_column(String(32), default="Orchestrator")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed|stopped
    workspace: Mapped[str] = mapped_column(String(500), default="")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # department | single | plan
    model: Mapped[str | None] = mapped_column(String(240), nullable=True)  # "provider:model" for the lead; None = auto
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)  # workspace before the run
    end_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ... and after it
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentRunEvent(Base):
    """What happened in a run: agents starting/finishing, tool calls, texts, files, key rotations."""

    __tablename__ = "agent_run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Chat(Base):
    """A conversation with the API department: every user message is a run in the chat's
    workspace; the lead agent sees the earlier messages and answers."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    workspace: Mapped[str] = mapped_column(String(500), default="")
    repo: Mapped[str | None] = mapped_column(String(300), nullable=True)  # "owner/name" on GitHub
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)  # the work branch
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
