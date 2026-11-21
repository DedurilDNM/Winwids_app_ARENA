"""Pydantic v2 schemas for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

# Pragmatic email validation: exactly one '@', non-empty local/domain parts.
# Deliberately allows single-label domains such as "admin@local" (the seeded admin).
import re as _re

_EMAIL_RE = _re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")


def _validate_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("Invalid email address")
    return value


EmailAddress = Annotated[str, AfterValidator(_validate_email)]

RoleName = Literal["Architect", "Coder", "Reviewer", "Tester", "Integrator", "ImagePrompter", "Orchestrator"]
AccountStatus = Literal["pending_login", "active", "relogin_needed", "disabled"]
ProjectStatus = Literal["draft", "running", "completed", "failed"]
TaskStatus = Literal["pending", "running", "done", "blocked", "failed"]


# --- Auth ---

class RegisterRequest(BaseModel):
    email: EmailAddress
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=4, max_length=64)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        return value.strip().lower()


class LoginRequest(BaseModel):
    email: EmailAddress
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailAddress
    is_admin: bool
    created_at: datetime


# --- Accounts ---

class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    service_note: str = Field(default="", max_length=255)
    default_role: RoleName = "Coder"
    github_repos: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("github_repos")
    @classmethod
    def _validate_repos(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for repo in value:
            repo = repo.strip()
            if not repo:
                continue
            if "/" not in repo:
                raise ValueError(f"Repository must look like 'owner/name', got: {repo!r}")
            cleaned.append(repo)
        return cleaned


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    service_note: str | None = Field(default=None, max_length=255)
    default_role: RoleName | None = None
    github_repos: list[str] | None = Field(default=None, max_length=20)
    status: AccountStatus | None = None

    @field_validator("github_repos")
    @classmethod
    def _validate_repos(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return AccountCreate._validate_repos(value)


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    service_note: str
    default_role: str
    github_repos: list[str]
    status: str
    last_health_check: datetime | None
    created_at: datetime


class AccountCreateResponse(AccountOut):
    """Returned by POST /api/accounts.

    `vnc_url` is present when a VNC session was started successfully.
    `vnc_error` explains why the VNC stack was unavailable (account still
    created, VNC can be retried via POST /api/accounts/{id}/vnc/start).
    """

    vnc_url: str | None = None
    headless: bool | None = None
    vnc_error: str | None = None


class VncStartResponse(BaseModel):
    live: bool = True
    headless: bool
    vnc_url: str | None = None


class VncInfoResponse(BaseModel):
    live: bool
    headless: bool = False
    vnc_url: str | None = None


# --- Projects ---

class ProjectAssignmentIn(BaseModel):
    account_id: int
    role: RoleName


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    task: str = Field(min_length=1, max_length=20_000)
    github_repo: str = Field(min_length=3, max_length=300)
    orchestrator_account_id: int | None = None
    assignments: list[ProjectAssignmentIn] = Field(default_factory=list, max_length=50)

    @field_validator("github_repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        value = value.strip()
        if "/" not in value:
            raise ValueError("github_repo must look like 'owner/name'")
        return value


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    task: str
    github_repo: str
    orchestrator_account_id: int | None
    status: str
    result: str | None
    work_branch: str | None = None
    pr_url: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    assignments: list[ProjectAssignmentIn] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    """PATCH body. Only editable while the project is not running.

    `orchestrator_account_id` / `assignments` use model_fields_set to tell
    "not provided" apart from "explicitly set to null/empty".
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    task: str | None = Field(default=None, min_length=1, max_length=20_000)
    github_repo: str | None = Field(default=None, min_length=3, max_length=300)
    orchestrator_account_id: int | None = None
    assignments: list[ProjectAssignmentIn] | None = Field(default=None, max_length=50)

    @field_validator("github_repo")
    @classmethod
    def _validate_repo(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if "/" not in value:
            raise ValueError("github_repo must look like 'owner/name'")
        return value


class ProjectDetailOut(ProjectOut):
    """Project + its tasks (timeline source for the client)."""

    tasks: list[TaskOut] = Field(default_factory=list)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    role: str
    task: str
    assigned_account_id: int | None
    status: str
    branch: str | None
    summary: str | None = None
    commit_sha: str | None = None
    created_at: datetime
    updated_at: datetime


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
