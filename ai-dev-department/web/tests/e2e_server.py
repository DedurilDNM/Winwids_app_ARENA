"""Run the real API for e2e tests with Chromium standing in for Camoufox.

Everything else is real: Xvfb, x11vnc, websockify, noVNC, storage_state
encryption and restore. Usage (from server/): python ../web/tests/e2e_server.py
Env: E2E_CHROMIUM=/path/to/chrome (Playwright's Chromium build).
"""

import os
import sys

sys.path.insert(0, os.getcwd())

from playwright.async_api import async_playwright  # noqa: E402

from app.services import browser_manager as bm  # noqa: E402


class ChromiumAsCamoufox:
    """Duck-types AsyncCamoufox: start() -> Browser, stop()."""

    def __init__(self, headless=True, proxy=None, env=None, window=None, **_):
        self._kw = {"headless": headless, "executable_path": os.environ["E2E_CHROMIUM"],
                    "args": ["--no-sandbox", "--no-first-run", "--start-maximized"]}
        if env:
            self._kw["env"] = env
        if window:
            self._kw["args"].append(f"--window-size={window[0]},{window[1]}")
        self._pw = None

    async def start(self):
        self._pw = await async_playwright().start()
        return await self._pw.chromium.launch(**self._kw)

    async def stop(self):
        if self._pw:
            await self._pw.stop()


bm.AsyncCamoufox = ChromiumAsCamoufox

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
