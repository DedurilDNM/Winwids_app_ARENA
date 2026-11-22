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
    # Where projects run. "api" (default): inside the API process, which owns
    # the account browsers - live WebSocket events and the user can watch the
    # agents work over VNC. "arq": a separate worker process (no live events,
    # separate headless browsers) - only for multi-server setups.
    PROJECT_RUNNER: str = "api"

    # CI (GitHub Actions) after every round: wait for the run of the latest commit
    CI_TIMEOUT_SECONDS: float = 1200
    CI_POLL_SECONDS: float = 15
    CI_START_TIMEOUT_SECONDS: float = 180
    GITHUB_API_URL: str = "https://api.github.com"

    # --- GitHub watcher / orchestration (Phase 3) ---
    # Optional server-side token for polling /status/*.md (public repos work
    # without one, rate-limited). A per-user ConnectorCredential overrides it.
    GITHUB_TOKEN: str = ""
    # GitHub API is not service traffic; proxy only if your network needs it.
    GITHUB_PROXY_URL: str = ""
    GITHUB_POLL_INTERVAL_SECONDS: int = 15
    TASK_TIMEOUT_SECONDS: int = 3600          # max wait for one agent task
    ORCHESTRATOR_TIMEOUT_SECONDS: int = 600   # wait_response timeout for orchestrator
    # Agents: give up when the service shows NO activity for this long (while it
    # works or the reply grows, the wait goes on, up to ADAPTER_MAX_REPLY_SECONDS).
    AGENT_TIMEOUT_SECONDS: int = 600
    # Session health cron runs every 5 minutes (arq cron in workers/settings.py).

    # --- VNC / noVNC (Phase 2) ---
    # Base URL the client should open for a VNC session. In production the path
    # /novnc is mapped by cloudflared/nginx to the local websockify instance.
    NOVNC_PUBLIC_BASE_URL: str = "http://127.0.0.1:6080"
    # Static files for the noVNC web client (Debian package `novnc`).
    NOVNC_WEB_ROOT: str = "/usr/share/novnc"
    WEBSOCKIFY_HOST: str = "127.0.0.1"
    WEBSOCKIFY_PORT: int = 6080
    XVFB_BASE_DISPLAY: int = 97          # per-account displays start here
    VNC_SHARED_DISPLAY: int = 99         # shared display used for background account restore
    VNC_BASE_RFB_PORT: int = 5901        # x11vnc ports start here
    VNC_WIDTH: int = 1600
    VNC_HEIGHT: int = 900
    # Test/CI only: skip Xvfb/x11vnc/noVNC, launch Camoufox headless.
    VNC_HEADLESS: bool = False

    # --- External AI service adapter (Phase 4) ---
    # CSS / Playwright selectors for interacting with the chat UI of the
    # external AI service. Adjust these to match your service's DOM. The
    # adapter never hard-codes a service name - everything is driven by
    # SERVICE_URL + these selectors.
    ADAPTER_INPUT_SELECTOR: str = "textarea, [contenteditable='true'], input[type='text']"
    ADAPTER_SEND_SELECTOR: str = "button[type='submit'], [aria-label*='send' i]"
    ADAPTER_MESSAGE_SELECTOR: str = "[role='article'], .message, .chat-message"
    ADAPTER_STATE_SELECTOR: str = "[aria-busy='true'], .generating, .loading, .error"
    ADAPTER_LOGIN_SELECTOR: str = "input[type='password'], [aria-label*='login' i]"
    # Auto-login (accounts with a login e-mail + password): the button that opens
    # the login form, the e-mail and password fields, the buttons that submit a
    # step (tried in order, "||"-separated; Enter is pressed first anyway).
    ADAPTER_LOGIN_OPEN_SELECTOR: str = ('button:text-is("Log in") || a:text-is("Log in") || '
                                        'button:has-text("Log in") || a:has-text("Log in")')
    ADAPTER_LOGIN_EMAIL_SELECTOR: str = ('input[type="email"], input[name="email"], input[autocomplete="email"], '
                                         'input[autocomplete="username"]')
    ADAPTER_LOGIN_PASSWORD_SELECTOR: str = 'input[type="password"]'
    ADAPTER_LOGIN_SUBMIT_SELECTOR: str = ('[role="dialog"] button[type="submit"] || button[type="submit"] || '
                                          'button:text-is("Continue") || [role="dialog"] button:text-is("Log in") || '
                                          'button:text-is("Log in")')
    # At most this many auto-login attempts per account per hour (then: ask a human).
    ADAPTER_AUTO_LOGIN_PER_HOUR: int = 2
    # Model check (accounts with a model set): before each task the model button
    # must show the model's name. ADAPTER_MODEL_CLICKS ("||"-separated, {model}
    # = the name) picks it; empty = the user picks it once in the account's browser.
    ADAPTER_MODEL_CHECK: bool = True
    # Pick the account's model automatically in the model picker (the button
    # showing a model name; a search box is used if present).
    ADAPTER_MODEL_AUTO: bool = True
    # Multi-select model lists: the option that clears the selection first.
    ADAPTER_MODEL_CLEAR_LABEL: str = "Deselect all"
    # Chat toolbar setup by visible labels, before every new chat:
    # ADAPTER_CHAT_MODE - the mode to select (e.g. Chat); ADAPTER_CHAT_MODES - all
    # labels the mode button can show; ADAPTER_MODALITY - the option of the menu
    # next to it (e.g. Code). Empty = do not touch.
    ADAPTER_CHAT_MODE: str = ""
    ADAPTER_CHAT_MODES: str = "Chat,Agent"
    ADAPTER_MODALITY: str = ""
    # If the model list is empty on a fresh page: open the latest chat and a new
    # one inside the app. Found automatically; these override the search.
    ADAPTER_SIDEBAR_SELECTOR: str = ""
    ADAPTER_HISTORY_LINK_SELECTOR: str = ""
    ADAPTER_NEW_CHAT_SELECTOR: str = ""
    ADAPTER_MODEL_CLICKS: str = ""
    # Extra selector that must appear before the chat is considered ready
    # (empty = skip this check).
    ADAPTER_READY_SELECTOR: str = ""
    # Optional: a button in a pop-up the service shows on send (e.g. a one-time
    # agreement). If it is visible after sending, the adapter clicks it and
    # sends again. Find it with `python -m app.tools.adapter_probe`.
    ADAPTER_DIALOG_ACCEPT_SELECTOR: str = ""
    # Optional clicks done in every new chat before the first message, in order,
    # separated by "||" - e.g. open a mode menu, then pick an option:
    # button[aria-label="Mode"] || [role="option"]:has-text("Agent")
    ADAPTER_SETUP_CLICKS: str = ""
    # URL that opens a NEW chat. Empty = SERVICE_URL. Placeholders: {base}, {chat_ref}.
    ADAPTER_NEW_CHAT_URL: str = ""
    # Captcha / pop-up / logout during a task: the task PAUSES and the user is
    # notified («нужна помощь»). 0 = wait for the human without a time limit
    # (until the project is stopped); > 0 = give up after that many seconds.
    ADAPTER_CAPTCHA_WAIT_SECONDS: float = 0
    # Longest total wait for one reply while the service keeps working (pauses
    # for a human do not count).
    ADAPTER_MAX_REPLY_SECONDS: float = 3600
    # A reply that stopped changing but does not look finished (an agent between
    # steps: no STATUS line yet) is taken only after this many seconds unchanged.
    ADAPTER_INCOMPLETE_SETTLE_SECONDS: float = 120
    # A reply that shows no new text for this long («thinking» forever): reload
    # the chat in place and read it again (the answer is often already there). 0 = off.
    ADAPTER_STUCK_REFRESH_SECONDS: float = 300
    # No message matches ADAPTER_MESSAGE_SELECTOR (e.g. another chat mode): read
    # the blocks right after our own sent message as the reply.
    ADAPTER_REPLY_FALLBACK: bool = True
    # After each reply: press Escape (closes rating/feedback panels) and click
    # ADAPTER_DISMISS_SELECTOR if it is visible.
    ADAPTER_AFTER_REPLY_ESCAPE: bool = True
    ADAPTER_DISMISS_SELECTOR: str = ""
    # Timing / resilience
    ADAPTER_POLL_INTERVAL_SECONDS: float = 2.0
    ADAPTER_NAVIGATE_TIMEOUT_SECONDS: float = 30.0
    ADAPTER_MAX_RETRIES: int = 3
    ADAPTER_RETRY_BACKOFF_SECONDS: float = 2.0

    # --- Tokens ---
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # --- HTTP / CORS ---
    # Comma-separated list. Dev default: localhost only. Prod: set to the client domain.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173"

    ENVIRONMENT: str = "development"

    # --- Web client (served by this API at /app/) ---
    WEB_ROOT: Path = SERVER_ROOT.parent / "web"

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
