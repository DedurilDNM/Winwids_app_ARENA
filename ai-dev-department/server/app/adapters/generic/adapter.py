"""Generic adapter stub.

Deliberately minimal: `is_alive` performs a real reachability check of
SERVICE_URL (through the context's browser), everything else raises
NotImplementedError. Replace this module with a real adapter for your
external AI service (implementing the ServiceAdapter protocol) and point
ADAPTER_MODULE at it. No concrete AI service is hard-coded here.
"""

from __future__ import annotations

from typing import Literal

from app.config import get_settings


class Adapter:
    """Stub implementation of the ServiceAdapter protocol."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def open_chat(self, context, chat_ref: str):
        raise NotImplementedError(
            "Generic adapter: open_chat is not implemented. "
            "Provide a real adapter via ADAPTER_MODULE."
        )

    async def send_message(self, page, text: str) -> None:
        raise NotImplementedError(
            "Generic adapter: send_message is not implemented. "
            "Provide a real adapter via ADAPTER_MODULE."
        )

    async def wait_response(self, page, timeout: int = 600) -> str:
        raise NotImplementedError(
            "Generic adapter: wait_response is not implemented. "
            "Provide a real adapter via ADAPTER_MODULE."
        )

    async def get_state(self, page) -> Literal["idle", "generating", "error", "login_required"]:
        # Per the MVP contract the generic adapter reports "idle".
        return "idle"

    async def is_alive(self, context) -> bool:
        """True if SERVICE_URL is reachable in the given browser context."""
        page = None
        try:
            page = await context.new_page()
            response = await page.goto(self.settings.SERVICE_URL, wait_until="domcontentloaded", timeout=30_000)
            return response is None or response.status < 500
        except Exception:
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass


adapter = Adapter()
