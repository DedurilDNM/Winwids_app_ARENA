"""FastAPI application entry point.

`uvicorn app.main:app --reload`

Lifespan: ensures DB schema exists and seeds the first admin user
(admin@local) with ADMIN_PASSWORD from the environment, or a random
one-time password printed to stdout.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from urllib.parse import urlsplit
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import accounts as accounts_api
from app.api import auth as auth_api
from app.api import attention as attention_api
from app.api import github as github_api
from app.api import calibrate as calibrate_api
from app.api import chats as chats_api
from app.engine import proxy as engine_proxy
from app.api import department as department_api
from app.api import keys as keys_api
from app.api import probe as probe_api
from app.api import projects as projects_api
from app.api import vnc as vnc_api
from app.api import ws as ws_api
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


def _novnc_origin() -> str:
    parts = urlsplit(get_settings().NOVNC_PUBLIC_BASE_URL)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


class WebSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security headers for the web client (/app/*).

    - CSP: only same-origin scripts/styles (no inline code), API/WebSocket on
      the same origin, and the noVNC origin allowed as an iframe source.
    - Referrer-Policy no-referrer: the noVNC iframe URL carries a VNC token.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        if request.url.path.startswith("/app"):
            frame_src = " ".join(x for x in ("'self'", _novnc_origin()) if x)
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data: blob:; connect-src 'self' ws: wss:; "
                f"frame-src {frame_src}; frame-ancestors 'self'; base-uri 'self'; "
                "form-action 'self'; object-src 'none'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            # every app file is revalidated (ETag): after an update the browser and any CDN in
            # front (e.g. Cloudflare caches .js/.css by default) never serve the old code
            response.headers["Cache-Control"] = "no-cache"
        return response


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


async def fail_interrupted_projects() -> int:
    """Projects left "running" by a previous process cannot continue (their
    in-process runner died with it): mark them failed so they can be restarted."""
    from datetime import datetime, timezone

    from app.models import Project, Task

    if settings.PROJECT_RUNNER == "arq":
        return 0  # an external worker may still be running them
    async with SessionLocal() as session:
        rows = (await session.scalars(select(Project).where(Project.status == "running"))).all()
        for project in rows:
            project.status = "failed"
            project.result = "Interrupted by a server restart. Start the project again."
            project.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.execute(
                Task.__table__.update()
                .where(Task.project_id == project.id, Task.status.in_(("pending", "running")))
                .values(status="failed")
            )
        await session.commit()
    if rows:
        logger.warning("Marked %d interrupted project(s) as failed", len(rows))
    return len(rows)


async def handle_interrupted_projects() -> list[int]:
    """Projects left "running" by a previous process: with AUTO_RESUME_INTERRUPTED
    they stay "running" and are continued after startup (returns their ids);
    otherwise they are marked failed."""
    from app.models import Project, Task

    if settings.PROJECT_RUNNER == "arq":
        return []
    if not settings.AUTO_RESUME_INTERRUPTED:
        await fail_interrupted_projects()
        return []
    async with SessionLocal() as session:
        rows = (await session.scalars(select(Project).where(Project.status == "running"))).all()
        for project in rows:
            project.result = "Interrupted by a server restart: continuing automatically"
            await session.execute(
                Task.__table__.update()
                .where(Task.project_id == project.id, Task.status.in_(("running", "paused")))
                .values(status="pending")
            )
        await session.commit()
        ids = [p.id for p in rows]
    if ids:
        logger.warning("%d project(s) were interrupted by the restart; they will continue: %s", len(ids), ids)
    return ids


async def resume_interrupted_later(project_ids: list[int], delay: float) -> None:
    """After startup (browsers restored): continue interrupted projects."""
    from app.workers.run_project import enqueue_run_project

    await asyncio.sleep(delay)
    for project_id in project_ids:
        try:
            await enqueue_run_project(project_id, resume=True)
            logger.info("Project %s: continued after the restart", project_id)
        except Exception:
            logger.exception("Project %s: could not continue after the restart", project_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_admin()
    interrupted = await handle_interrupted_projects()
    from app.services.disk import cleanup_browser_temp, low_disk

    try:
        cleanup_browser_temp()  # before any browser starts: leftovers of killed browsers fill /tmp
    except Exception:
        logger.warning("Browser temp cleanup failed", exc_info=True)
    if (disk := low_disk()):
        logger.error(disk)

    # Phase 2: relaunch browsers for all active accounts so they open logged-in
    # sessions without a fresh VNC login. Failures never block startup.
    # With the browser part off (BROWSER_ENABLED=false) nothing starts: no Xvfb, no browser.
    if settings.BROWSER_ENABLED:
        try:
            display = await vnc_manager.ensure_shared_display()
        except Exception as exc:
            logger.warning("Shared Xvfb display unavailable: %s", exc)
            display = None
        try:
            await browser_manager.restore_active_accounts(SessionLocal, display=display,
                                                          launcher=vnc_manager.launch_on_own_display)
        except Exception as exc:
            logger.warning("Active account restore failed: %s", exc)
    else:
        logger.info("The browser part is off (BROWSER_ENABLED=false): no browsers are started")
        interrupted = []  # browser projects do not continue by themselves

    logger.info("AI Dev Department API started (env=%s, service=%s)", settings.ENVIRONMENT, settings.SERVICE_DISPLAY_NAME)
    try:
        from app.agents.department import mark_interrupted

        if (n := await mark_interrupted()):
            logger.warning("%d API department run(s) were interrupted by the restart", n)
    except Exception:
        logger.warning("Could not mark interrupted runs", exc_info=True)
    checker_task = None
    if settings.KEYPOOL_CHECKER_ENABLED:  # limited API keys come back by themselves
        from app.agents.keypool import key_pool

        checker_task = asyncio.create_task(key_pool.run_checker())
    resume_task = None
    if interrupted:
        resume_task = asyncio.create_task(resume_interrupted_later(interrupted, settings.AUTO_RESUME_DELAY_SECONDS))
    yield
    if resume_task is not None and not resume_task.done():
        resume_task.cancel()
    if checker_task is not None:
        checker_task.cancel()

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
app.add_middleware(WebSecurityHeadersMiddleware)
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
app.include_router(projects_api.router)
app.include_router(github_api.router)
app.include_router(attention_api.router)
app.include_router(probe_api.router)
app.include_router(calibrate_api.router)
app.include_router(keys_api.router)
app.include_router(department_api.router)
app.include_router(chats_api.router)
app.include_router(engine_proxy.router)
app.include_router(ws_api.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/app", include_in_schema=False)
async def app_no_slash() -> RedirectResponse:
    # Relative asset URLs in index.html need the trailing slash.
    return RedirectResponse("/app/", status_code=307)


class VersionedStatic(StaticFiles):
    """The web client with a version in every code address (app.js?v=<build>, and each
    import inside the modules): a new build has new addresses, so no cache on the way
    (the browser, a CDN such as Cloudflare) can serve old code after an update."""

    _IMPORT = re.compile(r'(from\s+|import\s*\(\s*)"(\.{1,2}/[^"?]+\.js)"')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        root = Path(self.directory)
        stamp = hashlib.sha256()
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix in (".js", ".css", ".html"):
                st = f.stat()
                stamp.update(f"{f.relative_to(root)}:{st.st_size}:{st.st_mtime_ns}".encode())
        self.build = stamp.hexdigest()[:10]

    def rewrite(self, text: str, kind: str) -> str:
        v = self.build
        if kind == ".html":
            return text.replace('src="js/app.js"', f'src="js/app.js?v={v}"').replace(
                'href="css/app.css"', f'href="css/app.css?v={v}"')
        return self._IMPORT.sub(lambda m: f'{m.group(1)}"{m.group(2)}?v={v}"', text)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        file = getattr(response, "path", None)
        if response.status_code != 200 or not file:
            return response
        kind = Path(file).suffix
        if kind not in (".js", ".html") or Path(file).name == "sw.js":
            return response
        body = self.rewrite(Path(file).read_text(encoding="utf-8"), kind)
        media = "text/javascript" if kind == ".js" else "text/html"
        return Response(body, media_type=f"{media}; charset=utf-8")


if settings.WEB_ROOT.is_dir():
    app.mount("/app", VersionedStatic(directory=settings.WEB_ROOT, html=True), name="web")
else:  # pragma: no cover - server-only deployments
    logger.warning("Web client not found at %s; /app is disabled", settings.WEB_ROOT)


@app.get("/", tags=["meta"], response_model=None)
async def root(request: Request):
    # Browsers opening the bare domain land in the web app; API clients get JSON.
    if "text/html" in request.headers.get("accept", "") and settings.WEB_ROOT.is_dir():
        return RedirectResponse("/app/", status_code=307)
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
