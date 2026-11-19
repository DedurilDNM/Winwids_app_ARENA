"""WebSocket connection manager.

Broadcasts JSON events to clients subscribed to a project channel.
Used by the API (ws.py) and by background services (orchestrator,
session health checks).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self) -> None:
        # project_id -> set of connected sockets
        self._channels: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, project_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._channels.setdefault(project_id, set()).add(websocket)

    async def disconnect(self, project_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._channels.get(project_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._channels.pop(project_id, None)

    async def send_to_project(self, project_id: int, event: dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self._channels.get(project_id, set()))
        if not sockets:
            return
        payload = json.dumps(event, default=str)
        stale: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            await self.disconnect(project_id, ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        async with self._lock:
            project_ids = list(self._channels.keys())
        for project_id in project_ids:
            await self.send_to_project(project_id, event)


# Shared singleton used across the app and workers.
manager = WebSocketManager()
