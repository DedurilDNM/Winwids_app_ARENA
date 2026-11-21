"""Things that need a human right now (a captcha or a pop-up in an agent's
browser). In-memory, per user; shown on every page of the web app and pushed
as WebSocket events to the project screen."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

_items: dict[int, dict[str, dict]] = {}  # user_id -> key -> item
_ids = itertools.count(1)

KIND_TEXT = {
    "captcha": "the service is showing a captcha",
    "dialog": "a pop-up window blocks sending",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def raise_attention(user_id: int, *, account_id: int, account_name: str, project_id: int | None, kind: str) -> dict:
    key = f"{account_id}:{kind}"
    bucket = _items.setdefault(user_id, {})
    if key not in bucket:
        bucket[key] = {
            "id": next(_ids), "account_id": account_id, "account_name": account_name,
            "project_id": project_id, "kind": kind, "message": KIND_TEXT.get(kind, kind), "since": _now(),
        }
    return bucket[key]


def resolve_attention(user_id: int, *, account_id: int, kind: str) -> None:
    _items.get(user_id, {}).pop(f"{account_id}:{kind}", None)


def list_attention(user_id: int) -> list[dict]:
    return sorted(_items.get(user_id, {}).values(), key=lambda i: i["id"])
