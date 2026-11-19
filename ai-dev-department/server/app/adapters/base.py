"""Service adapter abstraction.

The concrete adapter is loaded from the ADAPTER_MODULE setting at runtime.
No concrete external AI service is named anywhere in this codebase.
"""

from __future__ import annotations

from typing import Literal, Protocol

# Playwright/Camoufox types are imported lazily by adapters to keep the base
# module importable without a running browser.
try:  # pragma: no cover - typing convenience only
    from playwright.async_api import BrowserContext, Page
except Exception:  # pragma: no cover
    BrowserContext = "BrowserContext"  # type: ignore[assignment]
    Page = "Page"  # type: ignore[assignment]


class ServiceAdapter(Protocol):
    async def open_chat(self, context: "BrowserContext", chat_ref: str) -> "Page": ...

    async def send_message(self, page: "Page", text: str) -> None: ...

    async def wait_response(self, page: "Page", timeout: int = 600) -> str: ...

    async def get_state(self, page: "Page") -> Literal["idle", "generating", "error", "login_required"]: ...

    async def is_alive(self, context: "BrowserContext") -> bool: ...


def load_adapter() -> ServiceAdapter:
    """Import the adapter module from settings and instantiate the adapter class.

    The module must expose a `GenericAdapter`/`Adapter` class or a module-level
    `adapter` instance. The default stub (app.adapters.generic.adapter) raises
    NotImplementedError on everything except is_alive.
    """
    import importlib

    from app.config import get_settings

    settings = get_settings()
    module_name = settings.ADAPTER_MODULE
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        # Tolerate the bare "adapters.generic.adapter" form from .env.example.
        module = importlib.import_module(f"app.{module_name}")

    for candidate in ("Adapter", "GenericAdapter"):
        cls = getattr(module, candidate, None)
        if cls is not None:
            return cls()

    instance = getattr(module, "adapter", None)
    if instance is not None:
        return instance

    raise RuntimeError(
        f"Adapter module {module_name!r} does not expose 'Adapter', 'GenericAdapter' or 'adapter'"
    )
