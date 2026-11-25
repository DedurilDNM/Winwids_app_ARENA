"""Screenshots of pages an agent made, so it can look at its own work (like a person would).

The page is served from the chat's workspace by a throwaway local HTTP server that serves
only that folder (no file:// - a page cannot pull server files into the picture), and the
browser refuses requests to local and private addresses. Chromium (Playwright) is used when
it is there, else the Firefox build of Camoufox the browser accounts already use.

Images go back to the model as image blocks (ToolOutput.images); small enough for any API.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import ipaddress
import logging
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

VIEWPORTS = {"desktop": (1440, 900, 1), "phone": (390, 844, 2)}
MAX_SIDE = 1280  # longer sides are scaled down: enough to read, ~1.3k tokens a picture
ENGINE = None  # tests: async (url, width, height, scale, full_page, out_path) -> None
_LOCK: dict[int, asyncio.Semaphore] = {}


class ScreenshotError(Exception):
    pass


class ToolOutput(str):
    """A tool's text result that also carries images [(media type, base64 data)]."""

    images: list[tuple[str, str]]

    def __new__(cls, text: str, images: list[tuple[str, str]] | None = None):
        obj = super().__new__(cls, text)
        obj.images = list(images or [])
        return obj


def image_block(path: Path) -> tuple[str, str, tuple[int, int]]:
    """(media type, base64, (width, height)) of an image file, scaled down when large."""
    data = path.read_bytes()
    kind = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/png")
    size = (0, 0)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            size = im.size
            if max(im.size) > MAX_SIDE or len(data) > 1_000_000:
                im = im.convert("RGB")
                im.thumbnail((MAX_SIDE, MAX_SIDE))
                out = io.BytesIO()
                im.save(out, "JPEG", quality=85)
                data, kind, size = out.getvalue(), "image/jpeg", im.size
    except Exception:  # no Pillow or an odd file: send it as it is (if not huge)
        if len(data) > 4_000_000:
            raise ScreenshotError("the image is too large to show") from None
    return kind, base64.b64encode(data).decode(), size


# ------------------------------------------------------------------ the workspace server

class _Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        root = Path(self.directory).resolve()
        target = Path(super().translate_path(path)).resolve()
        if target != root and root not in target.parents:  # a symlink out of the folder
            return str(root / "__outside__")
        return str(target)

    def guess_type(self, path):
        kind = super().guess_type(path)
        if kind.startswith("text/") or kind in ("application/javascript", "application/json", "image/svg+xml"):
            return f"{kind}; charset=utf-8"  # pages without <meta charset> still show Cyrillic right
        return kind

    def log_message(self, *args):  # quiet
        pass


class _Serve:
    def __init__(self, root: Path):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(root)))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _private(host: str) -> bool:
    if host in ("localhost", "") or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


# ------------------------------------------------------------------ browsers

async def _shoot(page, url, full_page, out, own):
    async def guard(route):
        parts = urlsplit(route.request.url)
        if parts.scheme in ("http", "https") and parts.netloc != own and _private(parts.hostname or ""):
            await route.abort()
        elif parts.scheme not in ("http", "https", "data", "blob"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", guard)
    await page.goto(url, wait_until="load", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(400)  # animations settle
    if full_page:
        height = await page.evaluate("document.documentElement.scrollHeight")
        vh = page.viewport_size["height"]
        await page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": page.viewport_size["width"],
                                                   "height": min(height, vh * 3)}, full_page=True)
    else:
        await page.screenshot(path=str(out))


def _chromium_candidates() -> list[str | None]:
    import shutil

    from app.config import get_settings

    out: list[str | None] = []
    set_path = getattr(get_settings(), "AGENT_SCREENSHOT_CHROMIUM", "") or os.environ.get("AGENT_SCREENSHOT_CHROMIUM", "")
    if set_path:
        out.append(set_path)
    out.append(None)  # Playwright's own build
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found and found not in out:
            out.append(found)
    return out


async def _capture(url, width, height, scale, full_page, out) -> None:
    if ENGINE is not None:
        return await ENGINE(url, width, height, scale, full_page, out)
    own = urlsplit(url).netloc
    errors = []
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = None
            for path in _chromium_candidates():  # the set one, Playwright's own, the system's
                try:
                    browser = await pw.chromium.launch(executable_path=path, args=["--no-sandbox"])
                    break
                except Exception as exc:
                    errors.append(f"chromium {path or 'playwright'}: {str(exc).splitlines()[0][:100]}")
            if browser is not None:
                try:
                    ctx = await browser.new_context(viewport={"width": width, "height": height},
                                                    device_scale_factor=scale, is_mobile=width < 600,
                                                    has_touch=width < 600)
                    await _shoot(await ctx.new_page(), url, full_page, out, own)
                    return
                finally:
                    await browser.close()
    except ImportError:
        errors.append("playwright is not installed")
    from app.config import get_settings

    if not get_settings().BROWSER_ENABLED:  # Camoufox only when the browser part is on
        raise ScreenshotError("no browser could take the screenshot (" + "; ".join(errors) + "; install Chromium: "
                              "server/.venv/bin/python -m playwright install chromium-headless-shell)")
    try:
        from camoufox.async_api import AsyncCamoufox

        async with AsyncCamoufox(headless=True) as browser:
            page = await browser.new_page()
            await page.set_viewport_size({"width": width, "height": height})
            await _shoot(page, url, full_page, out, own)
            return
    except ImportError:
        errors.append("camoufox is not installed")
    except Exception as exc:
        errors.append(f"firefox: {str(exc).splitlines()[0][:120]}")
    raise ScreenshotError("no browser could take the screenshot (" + "; ".join(errors) + ")")


async def take(workspace: Path, rel_page: str, out_dir: Path, *, viewports: list[str], full_page: bool) -> list[Path]:
    """Screenshots of workspace/rel_page for each viewport; the PNG files in out_dir."""
    loop_id = id(asyncio.get_running_loop())
    lock = _LOCK.setdefault(loop_id, asyncio.Semaphore(2))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(rel_page).stem if Path(rel_page).stem != "index" else (Path(rel_page).parent.name or "index")
    shots = []
    async with lock:
        with _Serve(workspace) as srv:
            base = f"http://127.0.0.1:{srv.port}/"
            for name in viewports:
                width, height, scale = VIEWPORTS[name]
                out = out_dir / f"{stem}-{name}.png"
                await asyncio.wait_for(_capture(base + quote(rel_page), width, height, scale, full_page, out), 90)
                shots.append(out)
    return shots
