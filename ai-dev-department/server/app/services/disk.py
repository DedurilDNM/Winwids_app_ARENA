"""Free disk space: a full disk breaks Xvfb («Failed to activate virtual core
keyboard»), the browser profile («Unable to restore IndexedDB ... unrelated to the
database») and logs - with errors that do not say «disk full». Say it plainly."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_FREE_MB = 300


def _paths() -> list[Path]:
    from app.config import DATA_DIR

    return [Path("/tmp"), Path(DATA_DIR), Path(os.environ.get("HOME") or "/tmp")]


def low_disk() -> str | None:
    """A user-facing sentence if a place the app writes to has < MIN_FREE_MB free, else None."""
    seen: set[int] = set()
    for path in _paths():
        try:
            if not path.exists():
                continue
            dev = path.stat().st_dev
            if dev in seen:
                continue
            seen.add(dev)
            free_mb = shutil.disk_usage(path).free // (1024 * 1024)
        except OSError:
            continue
        if free_mb < MIN_FREE_MB:
            return (f"The server disk is (almost) full: {free_mb} MB free on {path}. Free some space "
                    "(old logs, browser caches) and try again.")
    return None


def explain(error: str) -> str:
    """Add «disk full» to an error message when that is the real cause."""
    hint = low_disk()
    return f"{hint} ({error})" if hint else error
