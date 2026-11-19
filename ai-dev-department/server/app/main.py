"""FastAPI application entry point.

`uvicorn app.main:app --reload`

Lifespan: ensures DB schema exists and seeds the first admin user
(admin@local) with ADMIN_PASSWORD from the environment, or a random
one-time password printed to stdout.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import accounts as accounts_api
from app.api import auth as auth_api
from app.api import vnc as vnc_api
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.deps import limiter
from app.models import User
from app.security import hash_password
from app.services.browser_manager import browser_manager
from app.services.vnc_manager import vnc_manager
from sqlalchemy import func, select

logger = logging.getLogger("ai_dev_department")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = get_settings()

# Keys whose values must never appear in logs (case-insensitive).
SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "token", "access_token", "refresh_token",
    "secret", "authorization", "cookie", "set-cookie", "api_key", "apikey",
    "key", "invite_code",
}


def _mask_query(query: str) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in SENSITIVE_KEYS else v) for k, v in pairs]
    return urlencode(masked)


class MaskSensitiveDataMiddleware(BaseHTTPMiddleware):
    """Logs request method + path with sensitive query params masked.

    Bodies are intentionally never logged (they carry passwords).
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        masked_query = _mask_query(request.url.query)
        path = request.url.path + (f"?{masked_query}" if masked_query else "")
        logger.info("%s %s", request.method, path)
        return await call_next(request)


async def seed_admin() -> None:
    """Create admin@local on the very first run.

    Uses ADMIN_PASSWORD if set, otherwise a random one-time password that is
    printed to stdout exactly once. No password is ever logged afterwards.
    """
    async with SessionLocal() as session:
        user_count = await session.scalar(select(func.count()).select_from(User))
        if user_count and user_count > 0:
            return

        settings_ = get_settings()
        password = settings_.ADMIN_PASSWORD or secrets.token_urlsafe(12)
        admin = User(email="admin@local", hashed_password=hash_password(password), is_admin=True)
        session.add(admin)
        await session.commit()

        if settings_.ADMIN_PASSWORD:
            logger.info("Seeded initial admin user 'admin@local' (password from ADMIN_PASSWORD env).")
        else:
            # Printed exactly once, on first run only. flush=True because the
            # process may be stopped before stdout buffers drain.
            print("=" * 72, flush=True)
            print("INITIAL ADMIN CREATED", flush=True)
            print("  email:    admin@local", flush=True)
            print(f"  password: {password}", flush=True)
            print("Store it now - it will not be shown again. Change it after first login.", flush=True)
            print("=" * 72, flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_admin()

    # Phase 2: relaunch browsers for all active accounts so they open logged-in
    # sessions without a fresh VNC login. Failures never block startup.
    try:
        display = await vnc_manager.ensure_shared_display()
    except Exception as exc:
        logger.warning("Shared Xvfb display unavailable: %s", exc)
        display = None
    try:
        await browser_manager.restore_active_accounts(SessionLocal, display=display)
    except Exception as exc:
        logger.warning("Active account restore failed: %s", exc)

    logger.info("AI Dev Department API started (env=%s, service=%s)", settings.ENVIRONMENT, settings.SERVICE_DISPLAY_NAME)
    yield

    # Shutdown: stop browsers, Xvfb displays and websockify.
    try:
        await vnc_manager.stop_all()
    except Exception as exc:
        logger.warning("VNC shutdown failed: %s", exc)
    try:
        await browser_manager.shutdown()
    except Exception as exc:
        logger.warning("Browser shutdown failed: %s", exc)


app = FastAPI(
    title="AI Dev Department API",
    version=__version__,
    description="Control plane for a team of AI agents working on a shared project. "
    "The external AI service is configured exclusively through SERVICE_URL / SERVICE_DISPLAY_NAME.",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(MaskSensitiveDataMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)

app.include_router(auth_api.router)
app.include_router(accounts_api.router)
app.include_router(vnc_api.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "service": "AI Dev Department API",
        "version": __version__,
        "external_service": settings.SERVICE_DISPLAY_NAME,
        "docs": "/docs",
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals (which could include secrets) to the client.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
