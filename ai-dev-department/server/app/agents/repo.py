"""A chat's GitHub repository: clone into the workspace, a commit after each turn, push.

The token is given to git only for the one command (an HTTP header), so it is never
written into .git/config and the agents' shell commands never see it. Our own files
in the workspace (.aidd-git snapshots, the agents' home, the user's uploads) are kept
out of the project's history through .git/info/exclude.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import get_settings

EXCLUDE = [".aidd-git/", ".home/", "uploads/", "screenshots/"]  # never committed to the user's repository


class RepoError(RuntimeError):
    pass


def _auth(token: str | None) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def remote_url(repo: str) -> str:
    return f"{get_settings().GITHUB_GIT_URL.rstrip('/')}/{repo}.git"


def _git(ws: Path, *args: str, token: str | None = None, timeout: float = 300) -> str:
    cmd = ["git", *_auth(token), "-c", "user.name=AIDD", "-c", "user.email=aidd@localhost",
           "-c", "commit.gpgsign=false", *args]
    try:
        out = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout, errors="replace",
                             env={"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(ws)})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoError(f"git: {type(exc).__name__}") from exc
    if out.returncode != 0:
        message = (out.stderr or out.stdout).strip()
        if token:
            message = message.replace(token, "***")
        raise RepoError(message[-400:] or f"git {args[0]} failed")
    return out.stdout


def clone(ws: Path, repo: str, branch: str, token: str | None, base: str | None = None) -> str:
    """Clone `repo` into the (empty) workspace on a new branch. Returns the base branch."""
    ws.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="clone-", dir=ws.parent))
    try:
        args = ["clone", "--depth", "50", "--no-single-branch"]
        if base:
            args += ["--branch", base]
        _git(ws.parent, *args, remote_url(repo), str(tmp / "repo"), token=token)
        for item in (tmp / "repo").iterdir():
            target = ws / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    base_branch = _git(ws, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(ws, "checkout", "-q", "-b", branch)
    _ensure_exclude(ws)
    return base_branch


def _ensure_exclude(ws: Path) -> None:
    exclude = ws / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [e for e in EXCLUDE if e not in have]
    if missing:
        exclude.write_text("\n".join(have + missing) + "\n")


def is_repo(ws: Path) -> bool:
    return (ws / ".git").is_dir()


def commit_turn(ws: Path, message: str) -> dict | None:
    """Commit what the turn changed (if anything). Returns {sha, files} or None."""
    if not is_repo(ws):
        return None
    _ensure_exclude(ws)  # chats cloned before an entry was added get it too
    _git(ws, "add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ws).returncode == 0:
        return None
    files = [f for f in _git(ws, "diff", "--cached", "--name-only").splitlines() if f]
    _git(ws, "commit", "-q", "--no-verify", "-m", message)
    return {"sha": _git(ws, "rev-parse", "HEAD").strip(), "files": len(files)}


def push(ws: Path, branch: str, token: str | None) -> None:
    _git(ws, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", token=token)


def ahead(ws: Path, base_branch: str) -> int:
    """Commits on the work branch that the base branch does not have."""
    if not is_repo(ws):
        return 0
    try:
        return int(_git(ws, "rev-list", "--count", f"origin/{base_branch}..HEAD").strip() or 0)
    except RepoError:
        return 0
