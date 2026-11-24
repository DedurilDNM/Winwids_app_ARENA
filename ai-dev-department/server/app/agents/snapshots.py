"""Workspace snapshots for «what did this turn change».

A separate git store in <workspace>/.aidd-git (work tree = the workspace) records the
state before and after each run. The project's own .git (a cloned repository) is never
touched: our store ignores it. Changes = added/deleted lines per file between two
snapshots (or a snapshot and the current files), and the unified diff of one file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

STORE = ".aidd-git"
IGNORE = [".git/", f"{STORE}/", ".home/", "node_modules/", ".venv/", "venv/", "__pycache__/", ".cache/",
          ".pytest_cache/", ".next/", "*.pyc"]


class SnapshotError(RuntimeError):
    pass


def _git(ws: Path, *args: str, timeout: float = 120) -> str:
    cmd = ["git", f"--git-dir={ws / STORE}", f"--work-tree={ws}", "-c", "user.name=AIDD",
           "-c", "user.email=aidd@localhost", "-c", "core.quotepath=off", "-c", "core.autocrlf=false",
           "-c", "commit.gpgsign=false", *args]
    try:
        out = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"git: {type(exc).__name__}") from exc
    if out.returncode != 0:
        raise SnapshotError((out.stderr or out.stdout).strip()[:300])
    return out.stdout


def _ensure(ws: Path) -> None:
    store = ws / STORE
    if (store / "HEAD").exists():
        return
    ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(store)], capture_output=True, timeout=60, check=False)
    (store / "info").mkdir(exist_ok=True)
    (store / "info" / "exclude").write_text("\n".join(IGNORE) + "\n")


def snapshot(ws: Path, message: str) -> str:
    """Record every file of the workspace now; returns the commit id."""
    _ensure(ws)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    return _git(ws, "rev-parse", "HEAD").strip()


def changes(ws: Path, base: str, end: str | None = None) -> list[dict]:
    """[{path, added, deleted, binary}] between `base` and `end` (default: the files now)."""
    if not (ws / STORE / "HEAD").exists() or not base:
        return []
    if end:
        out = _git(ws, "diff", "--numstat", "-M", base, end)
    else:
        _git(ws, "add", "-A", "--intent-to-add")  # new files show up too
        out = _git(ws, "diff", "--numstat", "-M", base)
    result = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[-1]
        binary = added == "-"
        result.append({"path": path, "added": 0 if binary else int(added), "deleted": 0 if binary else int(deleted),
                       "binary": binary})
    return sorted(result, key=lambda c: c["path"])


def diff(ws: Path, base: str, path: str, end: str | None = None) -> str:
    if not (ws / STORE / "HEAD").exists() or not base:
        return ""
    if end:
        return _git(ws, "diff", "-M", base, end, "--", path)
    _git(ws, "add", "-A", "--intent-to-add")
    return _git(ws, "diff", "-M", base, "--", path)
