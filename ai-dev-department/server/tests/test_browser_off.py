"""With the browser part off (the default: BROWSER_ENABLED=false) no Camoufox is ever started:
not for an account, not on startup, not for an agent's screenshot."""

from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.services import browser_manager as bm_module


def test_no_browser_starts_when_the_browser_part_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "BROWSER_ENABLED", False)
    with pytest.raises(bm_module.BrowserUnavailableError, match="BROWSER_ENABLED"):
        asyncio.run(bm_module.browser_manager.launch(12345))

    from app.agents import screenshot

    monkeypatch.setattr(screenshot, "_chromium_candidates", lambda: ["/nonexistent/chrome"])
    (tmp_path / "p.html").write_text("<p>x</p>")
    with pytest.raises(screenshot.ScreenshotError, match="chromium-headless-shell"):
        asyncio.run(screenshot.take(tmp_path, "p.html", tmp_path / "shots", viewports=["desktop"], full_page=False))


def test_startup_restores_no_accounts_when_off(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    calls = []

    async def restore(*a, **kw):
        calls.append(1)
        return 0

    monkeypatch.setattr(get_settings(), "BROWSER_ENABLED", False)
    monkeypatch.setattr(bm_module.browser_manager, "restore_active_accounts", restore)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert calls == []


def test_the_default_is_off():
    from app.config import Settings

    assert Settings.model_fields["BROWSER_ENABLED"].default is False
