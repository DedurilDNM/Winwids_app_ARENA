"""VNC session manager: Xvfb + x11vnc + noVNC (websockify).

Flow per account:
  1. Xvfb :<display>                      (one display per account, or shared for restore)
  2. Camoufox launched with DISPLAY set   (browser_manager)
  3. x11vnc -display :<display> -rfbport <port> -localhost
  4. one global websockify serves noVNC + token-routed backends
     (--token-plugin TokenFile, --token-source tokens.cfg)

The token in the noVNC URL is a short-lived JWT (type=vnc, sub=account_id).
websockify routes based on the exact token string stored in the token file;
the token file is rewritten on every session change. Tokens expire after
VNC_TOKEN_TTL_MINUTES and are removed when the session stops.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from app.config import DATA_DIR, get_settings
from app.security import _create_token  # reuse JWT signing with a different type

logger = logging.getLogger(__name__)

VNC_DIR = DATA_DIR / "vnc"
TOKENS_FILE = VNC_DIR / "tokens.cfg"
VNC_LOG_DIR = VNC_DIR / "logs"
VNC_TOKEN_TTL_MINUTES = 120


class VncUnavailableError(RuntimeError):
    """Raised when the VNC stack (Xvfb/x11vnc/websockify) or noVNC files are missing."""


@dataclass
class VncSession:
    account_id: int
    token: str
    display: int | None = None
    rfb_port: int | None = None
    headless: bool = False
    xvfb: subprocess.Popen | None = None
    x11vnc: subprocess.Popen | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class VncManager:
    def __init__(self) -> None:
        self._sessions: dict[int, VncSession] = {}
        self._lock = asyncio.Lock()
        self._websockify: subprocess.Popen | None = None
        self._shared_xvfb: subprocess.Popen | None = None
        # Displays whose VNC was closed but whose browser keeps running there
        # (after confirm-login / closing the viewer): account_id -> (display, Xvfb).
        self._parked: dict[int, tuple[int, subprocess.Popen]] = {}

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _which_or_raise(name: str) -> str:
        path = shutil.which(name)
        if not path:
            raise VncUnavailableError(
                f"{name} is not installed on this server. Run deploy/install.sh."
            )
        return path

    @staticmethod
    def _novnc_web_root() -> Path:
        settings = get_settings()
        root = Path(settings.NOVNC_WEB_ROOT)
        if not root.is_dir():
            raise VncUnavailableError(
                f"noVNC web root not found at {root}. Install the 'novnc' package "
                "or clone https://github.com/novnc/noVNC there (see deploy/install.sh)."
            )
        if not (root / "vnc.html").exists():
            raise VncUnavailableError(f"noVNC web root {root} does not contain vnc.html")
        return root

    def _free_display(self) -> int:
        settings = get_settings()
        used = {s.display for s in self._sessions.values() if s.display is not None}
        for display in range(settings.XVFB_BASE_DISPLAY, settings.XVFB_BASE_DISPLAY + 30):
            if display in used:
                continue
            if Path(f"/tmp/.X11-unix/X{display}").exists():
                continue
            if Path(VNC_DIR / f"display-{display}.lock").exists():
                continue
            return display
        raise VncUnavailableError("No free Xvfb display numbers (97-126)")

    def _free_rfb_port(self) -> int:
        import socket

        settings = get_settings()
        used = {s.rfb_port for s in self._sessions.values() if s.rfb_port is not None}
        for port in range(settings.VNC_BASE_RFB_PORT, settings.VNC_BASE_RFB_PORT + 100):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise VncUnavailableError("No free RFB ports (5901-6000)")

    @staticmethod
    def _wait_x_socket(display: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(f"/tmp/.X11-unix/X{display}").exists():
                return
            time.sleep(0.1)
        raise VncUnavailableError(f"Xvfb :{display} did not create its socket in {timeout}s")

    @staticmethod
    def x_display_alive(display: int) -> bool:
        """True if an X server really answers on :display (not just a stale socket file)."""
        import socket

        path = f"/tmp/.X11-unix/X{display}"
        if not Path(path).exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(path)
            return True
        except OSError:
            return False

    @staticmethod
    def _clear_stale_display(display: int) -> None:
        """Remove the socket/lock files a crashed Xvfb left behind (they block a restart)."""
        for path in (Path(f"/tmp/.X11-unix/X{display}"), Path(f"/tmp/.X{display}-lock")):
            try:
                path.unlink(missing_ok=True)
                logger.warning("Removed stale X display file %s", path)
            except OSError as exc:
                logger.warning("Cannot remove stale X display file %s: %s", path, exc)

    @staticmethod
    def _wait_tcp_port(host: str, port: int, timeout: float = 10.0) -> bool:
        """Block until something listens on host:port. Returns False on timeout."""
        import socket

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    # ------------------------------------------------------- token plumbing

    @staticmethod
    def _ensure_dirs() -> None:
        VNC_DIR.mkdir(parents=True, exist_ok=True)
        VNC_LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not TOKENS_FILE.exists():
            TOKENS_FILE.write_text("", encoding="utf-8")
        try:
            TOKENS_FILE.chmod(0o600)
        except OSError:
            pass

    def _write_tokens(self) -> None:
        """Rewrite the websockify token file from the live session set."""
        self._ensure_dirs()
        lines = [f"{s.token}: 127.0.0.1:{s.rfb_port}" for s in self._sessions.values() if s.rfb_port]
        TOKENS_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        try:
            TOKENS_FILE.chmod(0o600)
        except OSError:
            pass

    def _create_token(self, account_id: int) -> str:
        # Short-lived JWT; validated by existence in the token file for routing,
        # and expiry is enforced when we build/rotate URLs (TTL above).
        return _create_token(account_id, "vnc", timedelta(minutes=VNC_TOKEN_TTL_MINUTES))  # type: ignore[arg-type]

    def vnc_url_for(self, session: VncSession) -> str:
        settings = get_settings()
        base = settings.NOVNC_PUBLIC_BASE_URL.rstrip("/")
        # noVNC < 1.5 (Debian's package) ignores a top-level `token` param, so
        # the token must travel inside `path` (the websocket path, relative to
        # the host root) for websockify's TokenFile plugin to route it.
        # `token` is kept as well for newer noVNC builds.
        prefix = urlsplit(base).path.strip("/")
        ws_path = f"{prefix}/websockify" if prefix else "websockify"
        path = quote(f"{ws_path}?token={session.token}", safe="")
        return (
            f"{base}/vnc.html?autoconnect=true&resize=scale"
            f"&path={path}&token={session.token}"
        )

    # ---------------------------------------------------------- websockify

    def _ensure_websockify(self) -> None:
        if self._websockify is not None and self._websockify.poll() is None:
            return
        settings = get_settings()
        self._novnc_web_root()  # raises if noVNC is missing
        self._ensure_dirs()

        binary = shutil.which("websockify")
        cmd = [binary] if binary else [sys.executable, "-m", "websockify"]
        cmd += [
            "--web", str(get_settings().NOVNC_WEB_ROOT),
            f"--token-plugin=websockify.token_plugins.TokenFile",
            f"--token-source={TOKENS_FILE}",
            f"{settings.WEBSOCKIFY_HOST}:{settings.WEBSOCKIFY_PORT}",
        ]
        log_file = open(VNC_LOG_DIR / "websockify.log", "ab")
        try:
            self._websockify = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise VncUnavailableError(f"websockify failed to start: {exc}") from exc
        logger.info("websockify started on %s:%s (pid=%s)", settings.WEBSOCKIFY_HOST, settings.WEBSOCKIFY_PORT, self._websockify.pid)

    def _stop_websockify(self) -> None:
        if self._websockify is not None and self._websockify.poll() is None:
            self._websockify.terminate()
            try:
                self._websockify.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._websockify.kill()
        self._websockify = None

    # ------------------------------------------------------------ sessions

    async def ensure_shared_display(self) -> str | None:
        """Start the shared Xvfb display (:99) used for background restore."""
        settings = get_settings()
        if settings.VNC_HEADLESS:
            return None
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            logger.warning("Xvfb not installed; shared display unavailable")
            return None
        if self._shared_xvfb is not None and self._shared_xvfb.poll() is None:
            return f":{settings.VNC_SHARED_DISPLAY}"
        display = settings.VNC_SHARED_DISPLAY
        if self.x_display_alive(display):
            return f":{display}"  # already running (e.g. left over from a previous process)
        self._clear_stale_display(display)
        self._shared_xvfb = subprocess.Popen(
            [xvfb, f":{display}", "-screen", "0", f"{settings.VNC_WIDTH}x{settings.VNC_HEIGHT}x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._wait_x_socket, display, 10.0)
        except VncUnavailableError:
            self._shared_xvfb.terminate()
            self._shared_xvfb = None
            raise
        return f":{display}"

    @staticmethod
    async def _ensure_service_page(account_id: int) -> None:
        """A browser restored in the background has no tab: show the service
        instead of an empty window (an existing tab - e.g. an agent's chat - is kept)."""
        from app.services.browser_manager import browser_manager

        context = browser_manager.get_context(account_id)
        if context is None:
            return
        try:
            if not [p for p in context.pages if not p.is_closed()]:
                page = await context.new_page()
                await page.goto(get_settings().SERVICE_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            logger.warning("Could not open the service page for account %s", account_id, exc_info=True)

    async def launch_on_own_display(self, account_id: int, storage_state: dict | None, *, open_url: bool = False):
        """Start a browser for an account on its OWN Xvfb display (parked, i.e.
        ready to be shown over VNC without touching the browser). Used when
        browsers are restored after a restart and when an agent needs one.
        Falls back to headless when Xvfb is not available."""
        from app.services.browser_manager import browser_manager

        settings = get_settings()
        context = browser_manager.get_context(account_id)
        if context is not None:
            return context
        xvfb_bin = shutil.which("Xvfb")
        if settings.VNC_HEADLESS or not xvfb_bin:
            return await browser_manager.launch(account_id, storage_state=storage_state, headless=True, open_url=open_url)
        async with self._lock:
            display = self._free_display()
            xvfb = subprocess.Popen(
                [xvfb_bin, f":{display}", "-screen", "0", f"{settings.VNC_WIDTH}x{settings.VNC_HEIGHT}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._wait_x_socket, display, 10.0)
                self._ensure_dirs()
                (VNC_DIR / f"display-{display}.lock").write_text(str(xvfb.pid), encoding="utf-8")
                context = await browser_manager.launch(account_id, storage_state=storage_state, display=f":{display}",
                                                       headless=False, open_url=open_url)
            except Exception:
                self._terminate(xvfb)
                (VNC_DIR / f"display-{display}.lock").unlink(missing_ok=True)
                raise
            self._parked[account_id] = (display, xvfb)
            return context

    async def start_session(self, account, open_url: bool = True) -> VncSession:
        """Start Xvfb + browser + x11vnc + websockify route for an account."""
        from app.services.browser_manager import browser_manager

        settings = get_settings()
        async with self._lock:
            existing = self._sessions.get(account.id)
            if existing is not None:
                return existing

            if settings.VNC_HEADLESS:
                storage_state = None
                if account.storage_state_encrypted:
                    import json

                    from app.security import decrypt_bytes

                    storage_state = json.loads(decrypt_bytes(account.storage_state_encrypted))
                await browser_manager.launch(
                    account.id, storage_state=storage_state, display=None, headless=True, open_url=open_url
                )
                session = VncSession(account_id=account.id, token="", headless=True)
                self._sessions[account.id] = session
                return session

            xvfb_bin = self._which_or_raise("Xvfb")
            x11vnc_bin = self._which_or_raise("x11vnc")
            self._novnc_web_root()  # validate early, before spawning anything

            rfb_port = self._free_rfb_port()
            parked = self._parked.pop(account.id, None)
            running_on = browser_manager.get_display(account.id)

            if parked and parked[1].poll() is None and running_on == f":{parked[0]}":
                # The browser is still alive on its own display: just re-attach
                # VNC to it (keeps the live page and login, no relaunch).
                display, xvfb = parked
                lock_file = VNC_DIR / f"display-{display}.lock"
            elif running_on and running_on.startswith(":") and self.x_display_alive(int(running_on[1:])):
                # Running on another display (e.g. a shared one): show it as it is.
                # NEVER relaunch a running browser - an agent may be working in it.
                if parked:
                    self._terminate(parked[1])
                    (VNC_DIR / f"display-{parked[0]}.lock").unlink(missing_ok=True)
                display, xvfb, lock_file = int(running_on[1:]), None, None
            else:
                if parked:
                    self._terminate(parked[1])
                    (VNC_DIR / f"display-{parked[0]}.lock").unlink(missing_ok=True)
                if browser_manager.is_running(account.id):
                    raise VncUnavailableError(
                        "The browser of this account runs without a screen right now (an agent may be working "
                        "in it); it cannot be shown. Try again after the task."
                    )
                storage_state = None
                if account.storage_state_encrypted:
                    import json

                    from app.security import decrypt_bytes

                    storage_state = json.loads(decrypt_bytes(account.storage_state_encrypted))

                display = self._free_display()
                xvfb = subprocess.Popen(
                    [xvfb_bin, f":{display}", "-screen", "0", f"{settings.VNC_WIDTH}x{settings.VNC_HEIGHT}x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    await asyncio.get_event_loop().run_in_executor(None, self._wait_x_socket, display, 10.0)
                except Exception:
                    xvfb.terminate()
                    raise

                lock_file = VNC_DIR / f"display-{display}.lock"
                self._ensure_dirs()
                lock_file.write_text(str(xvfb.pid), encoding="utf-8")

                try:
                    await browser_manager.launch(
                        account.id,
                        storage_state=storage_state,
                        display=f":{display}",
                        headless=False,
                        open_url=open_url,
                    )
                except Exception:
                    xvfb.terminate()
                    lock_file.unlink(missing_ok=True)
                    raise

            if open_url:
                await self._ensure_service_page(account.id)

            x11vnc = subprocess.Popen(
                [
                    x11vnc_bin,
                    "-display", f":{display}",
                    "-forever",
                    "-shared",
                    "-localhost",
                    "-nopw",
                    "-rfbport", str(rfb_port),
                    "-quiet",
                ],
                stdout=open(VNC_LOG_DIR / f"x11vnc-{account.id}.log", "ab"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            self._ensure_websockify()

            # The URL goes to the client right away: make sure both hops accept
            # connections first, otherwise the very first page load after a
            # server start hits a closed port.
            loop = asyncio.get_event_loop()
            if not await loop.run_in_executor(
                None, self._wait_tcp_port, settings.WEBSOCKIFY_HOST, settings.WEBSOCKIFY_PORT, 10.0
            ):
                logger.warning("websockify is not accepting connections on port %s yet", settings.WEBSOCKIFY_PORT)
            if not await loop.run_in_executor(None, self._wait_tcp_port, "127.0.0.1", rfb_port, 10.0):
                logger.warning("x11vnc for account %s is not listening on %s yet", account.id, rfb_port)

            token = self._create_token(account.id)
            session = VncSession(
                account_id=account.id,
                token=token,
                display=display,
                rfb_port=rfb_port,
                headless=False,
                xvfb=xvfb,
                x11vnc=x11vnc,
            )
            self._sessions[account.id] = session
            self._write_tokens()
            logger.info(
                "VNC session started for account %s (display=:%s, rfb=%s)", account.id, display, rfb_port
            )
            return session

    def get_session(self, account_id: int) -> VncSession | None:
        return self._sessions.get(account_id)

    async def stop_session(self, account_id: int, *, keep_browser: bool = True, kill_display: bool = True) -> None:
        """Stop the VNC session (x11vnc only by default; the browser stays alive).

        keep_browser=True matches the spec: after confirm-login the browser
        remains running in the background and only the noVNC path is closed.
        """
        from app.services.browser_manager import browser_manager

        async with self._lock:
            session = self._sessions.pop(account_id, None)
            if session is None:
                parked = self._parked.pop(account_id, None)
                if not keep_browser:
                    await browser_manager.stop(account_id)
                    if parked:
                        self._terminate(parked[1])
                        (VNC_DIR / f"display-{parked[0]}.lock").unlink(missing_ok=True)
                elif parked:
                    self._parked[account_id] = parked
                return

            if session.x11vnc is not None and session.x11vnc.poll() is None:
                session.x11vnc.terminate()
                try:
                    session.x11vnc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    session.x11vnc.kill()

            if keep_browser and session.display is not None and session.xvfb is not None:
                self._parked[account_id] = (session.display, session.xvfb)
            if not keep_browser:
                await browser_manager.stop(account_id)
                if session.xvfb is not None and session.xvfb.poll() is None:
                    session.xvfb.terminate()
                    try:
                        session.xvfb.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        session.xvfb.kill()
                if session.display is not None:
                    (VNC_DIR / f"display-{session.display}.lock").unlink(missing_ok=True)

            self._write_tokens()
            if not self._sessions:
                self._stop_websockify()
            logger.info("VNC session stopped for account %s (keep_browser=%s)", account_id, keep_browser)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    async def stop_all(self) -> None:
        for account_id in list(self._sessions) + list(self._parked):
            await self.stop_session(account_id, keep_browser=False)
        if self._shared_xvfb is not None and self._shared_xvfb.poll() is None:
            self._shared_xvfb.terminate()
            try:
                self._shared_xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._shared_xvfb.kill()
        self._shared_xvfb = None
        self._stop_websockify()


# Shared singleton.
vnc_manager = VncManager()
