"""Chromium standing in for Camoufox in e2e runs (same start()/stop() API)."""

import os

from playwright.async_api import async_playwright


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
