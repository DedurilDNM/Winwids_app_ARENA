"""Connector protocol.

A connector gives the server read access to an external tool's content
(files in a repo, documents, ...). The MVP ships with GitHub only; the
protocol is deliberately small so a Google Drive (or similar) connector can
be added in V2 without changing callers.
"""

from __future__ import annotations

from typing import Protocol


class Connector(Protocol):
    """Minimal read-only connector interface."""

    kind: str  # e.g. "github"

    async def read_file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        """Return the text content of `path` in `repo` at `ref`, or None if missing."""
        ...

    async def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[str]:
        """Return entry names of directory `path` in `repo` at `ref` (empty if missing)."""
        ...
