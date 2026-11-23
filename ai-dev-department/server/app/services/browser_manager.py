"""Camoufox browser context manager.

Owns the lifecycle of browser handles (one Camoufox instance per account).
Storage state (cookies + localStorage, optionally IndexedDB) is exported from
here and persisted *encrypted* by the API layer - this module never touches
the database and never logs cookies/tokens.

All traffic to the external AI service goes through PROXY_URL (browser-level
proxy). No concrete service is named anywhere in this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

logger = logging.getLogger(__name__)

try:  # Camoufox is expected to be installed on the server (Phase 2+).
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover - import-time guard
    AsyncCamoufox = None  # type: ignore[assignment]


class BrowserUnavailableError(RuntimeError):
    """Raised when a browser cannot be launched (camoufox missing / launch failed)."""


@dataclass
class BrowserHandle:
    account_id: int
    camoufox: Any
    browser: Any
    context: Any
    headless: bool
    display: str | None


def parse_proxy(proxy_url: str) -> dict[str, str] | None:
    """Convert http://user:pass@host:port into a Playwright proxy dict."""
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise BrowserUnavailableError(f"Unsupported PROXY_URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BrowserUnavailableError("PROXY_URL is missing a host")
    server = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    proxy: dict[str, str] = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def _has_indexed_db(state: dict) -> bool:
    return any(o.get("indexedDB") for o in (state.get("origins") or []) if isinstance(o, dict))


def without_indexed_db(state: dict) -> dict:
    """The saved session without IndexedDB (cookies + localStorage stay)."""
    origins = [{k: v for k, v in o.items() if k != "indexedDB"} for o in (state.get("origins") or [])
               if isinstance(o, dict)]
    return {**state, "origins": origins}


def _strip_indexed(state: dict) -> dict:
    """Drop every IndexedDB part of a saved session, whatever its key spelling."""
    origins = [{k: v for k, v in o.items() if "indexed" not in k.lower()} for o in (state.get("origins") or [])
               if isinstance(o, dict)]
    return {**{k: v for k, v in state.items() if "indexed" not in k.lower()}, "origins": origins}


class BrowserManager:
    def __init__(self) -> None:
        self._handles: dict[int, BrowserHandle] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _lock_for(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    @property
    def live_account_ids(self) -> list[int]:
        return list(self._handles.keys())

    def get_display(self, account_id: int) -> str | None:
        """X display the account's browser runs on (None: none running / headless)."""
        handle = self._handles.get(account_id)
        return handle.display if handle else None

    def is_running(self, account_id: int) -> bool:
        return account_id in self._handles

    def get_context(self, account_id: int):
        handle = self._handles.get(account_id)
        return handle.context if handle else None

    async def launch(
        self,
        account_id: int,
        *,
        storage_state: dict | None = None,
        display: str | None = None,
        headless: bool | None = None,
        open_url: bool = True,
    ):
        """Launch (or return the existing) browser for an account.

        If open_url is True the first page is navigated to SERVICE_URL so the
        manual VNC login can happen right away.
        """
        settings = get_settings()
        if headless is None:
            headless = settings.VNC_HEADLESS

        async with self._lock_for(account_id):
            existing = self._handles.get(account_id)
            if existing is not None:
                return existing.context

            if AsyncCamoufox is None:
                raise BrowserUnavailableError(
                    "camoufox is not installed. Run deploy/install.sh or `pip install -r requirements.txt` "
                    "and `python -m camoufox fetch` on the server."
                )

            kwargs: dict[str, Any] = {"headless": headless}
            proxy = parse_proxy(settings.PROXY_URL)
            if proxy:
                kwargs["proxy"] = proxy
            if display and not headless:
                env = dict(os.environ)
                env["DISPLAY"] = display
                kwargs["env"] = env
                kwargs["window"] = (settings.VNC_WIDTH, settings.VNC_HEIGHT)

            camoufox = AsyncCamoufox(**kwargs)
            try:
                browser = await camoufox.start()
                context = await self._new_context(browser, account_id, storage_state)
                if open_url:
                    page = await context.new_page()
                    await page.goto(settings.SERVICE_URL, wait_until="domcontentloaded", timeout=60_000)
            except BrowserUnavailableError:
                raise
            except Exception as exc:
                try:
                    await camoufox.stop()
                except Exception:
                    pass
                from app.services.disk import explain

                raise BrowserUnavailableError(
                    explain(f"Failed to launch browser for account {account_id}: {exc}")) from exc

            self._handles[account_id] = BrowserHandle(
                account_id=account_id,
                camoufox=camoufox,
                browser=browser,
                context=context,
                headless=headless,
                display=display,
            )
            logger.info("Browser launched for account %s (headless=%s, display=%s)", account_id, headless, display)
            return context

    @staticmethod
    async def _new_context(browser, account_id: int, storage_state: dict | None):
        """A context with the saved login. A part of the session that cannot be restored
        must not block the browser: first without IndexedDB, then cookies only, then empty
        (the user logs in again) - each step logged."""
        attempts = [("full session", storage_state)]
        if storage_state:
            attempts += [("without IndexedDB", _strip_indexed(storage_state)),
                         ("cookies only", {"cookies": storage_state.get("cookies") or [], "origins": []}),
                         ("empty (log in again)", None)]
        last: Exception | None = None
        for label, state in attempts:
            try:
                context = await browser.new_context(storage_state=state, no_viewport=True)
                if label != "full session":
                    logger.warning("Account %s: browser started with the %s (the saved session did not restore)",
                                   account_id, label)
                return context
            except Exception as exc:
                last = exc
                logger.warning("Account %s: new context with the %s failed: %s", account_id, label,
                               type(exc).__name__)
        raise last  # type: ignore[misc]

    async def export_storage_state(self, account_id: int) -> dict:
        """Dump the live context storage_state (cookies + localStorage, IndexedDB if supported)."""
        handle = self._handles.get(account_id)
        if handle is None:
            raise BrowserUnavailableError(f"No live browser session for account {account_id}")
        context = handle.context
        if not get_settings().BROWSER_SAVE_INDEXEDDB:
            try:
                return await context.storage_state()
            except Exception as exc:
                raise BrowserUnavailableError(
                    f"The account's browser does not respond ({type(exc).__name__}): open its browser again") from exc
        try:
            return await context.storage_state(indexed_db=True)  # Playwright >= 1.49 (experimental)
        except TypeError:
            return await context.storage_state()
        except Exception:
            # IndexedDB export fails on some pages: cookies + localStorage are enough to stay logged in
            logger.warning("storage_state with IndexedDB failed for account %s; saving without it", account_id,
                           exc_info=True)
            try:
                return await context.storage_state()
            except Exception as exc:
                raise BrowserUnavailableError(
                    f"The account's browser does not respond ({type(exc).__name__}): open its browser again") from exc

    async def stop(self, account_id: int, *, close_browser: bool = True) -> None:
        handle = self._handles.pop(account_id, None)
        if handle is None:
            return
        try:
            await handle.context.close()
        except Exception:
            logger.debug("context.close() failed for account %s", account_id, exc_info=True)
        try:
            await handle.camoufox.stop()
        except Exception:
            logger.debug("camoufox.stop() failed for account %s", account_id, exc_info=True)
        logger.info("Browser stopped for account %s", account_id)

    async def shutdown(self) -> None:
        for account_id in list(self._handles):
            await self.stop(account_id)

    async def restore_active_accounts(self, session_factory, display: str | None = None, launcher=None) -> int:
        """Relaunch browsers for all accounts with status=active and saved storage state.

        Called once on server startup. `display` is the shared Xvfb display used in
        non-headless mode (vnc_manager.ensure_shared_display). Failures are logged and
        counted - one broken account must not prevent the API from starting.
        """
        import json

        from sqlalchemy import select

        from app.config import get_settings
        from app.models import Account
        from app.security import decrypt_bytes

        settings = get_settings()
        restored = 0
        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(Account).where(Account.status == "active", Account.storage_state_encrypted.isnot(None))
                )
            ).all()

        for account in rows:
            try:
                storage_state = json.loads(decrypt_bytes(account.storage_state_encrypted))
                if launcher is not None:  # each account on its own display (can be shown over VNC safely)
                    await launcher(account.id, storage_state)
                    restored += 1
                    continue
                if settings.VNC_HEADLESS:
                    headless, use_display = True, None
                elif display:
                    headless, use_display = False, display
                else:
                    logger.warning("No display available; restoring account %s headless", account.id)
                    headless, use_display = True, None
                await self.launch(
                    account.id,
                    storage_state=storage_state,
                    display=use_display,
                    headless=headless,
                    open_url=False,
                )
                restored += 1
            except Exception as exc:
                logger.warning("Could not restore browser for account %s: %s", account.id, exc)
        if restored:
            logger.info("Restored %d active account browser(s) from storage state", restored)
        return restored


# Shared singleton.
browser_manager = BrowserManager()
