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
    import tempfile

    from app.config import DATA_DIR

    return [Path("/tmp"), Path(tempfile.gettempdir()), Path(DATA_DIR), Path(os.environ.get("HOME") or "/tmp")]


# Temporary browser profiles / Playwright artifacts. Every browser start makes one; a
# killed browser (service restart) leaves it behind - on a small /tmp that fills it.
_BROWSER_TEMP = ("playwright*", "rust_mozprofile*", "*camoufox*", "tmp*_profile*", "mozprofile*")


def cleanup_browser_temp() -> int:
    """At startup (no browser runs yet): remove OUR leftover browser temp dirs from the
    temp dirs. Only entries owned by this user. Returns the number removed."""
    import tempfile

    uid = os.getuid()
    removed = 0
    for base in {Path(tempfile.gettempdir()), Path("/tmp")}:
        for pattern in _BROWSER_TEMP:
            for path in base.glob(pattern):
                try:
                    if path.lstat().st_uid != uid:
                        continue
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
    if removed:
        logger.warning("Removed %d leftover browser temp dir(s)", removed)
    return removed


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
