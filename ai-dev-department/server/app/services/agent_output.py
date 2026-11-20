"""Parse an agent's chat reply into files to commit + a status.

Agents never touch GitHub; the server appends an output contract to every
agent prompt (see prompts.OUTPUT_CONTRACT) and commits what comes back:

    FILE: src/app.py
    ```python
    print("hi")
    ```
    STATUS: DONE
    SUMMARY: what was done (or why blocked)

The reply may arrive as raw markdown or as rendered text (headings without
`#`, code blocks without fences), so the parser accepts both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

MAX_FILES = 60
MAX_TOTAL_BYTES = 1_000_000
MAX_PATH_LEN = 300

_FILE_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?FILE:(?:\*\*)?\s*`?([^`\s]+?)`?\s*(?:\*\*)?\s*$")
_STATUS_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?STATUS:(?:\*\*)?\s*\**\s*(DONE|BLOCKED)\b\**\s*(.*)$", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^\s*(?:\*\*)?SUMMARY:(?:\*\*)?\s*(.*)$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


class AgentOutputError(ValueError):
    """The reply does not follow the output contract (message is shown to the agent)."""


@dataclass
class AgentOutput:
    status: str  # "DONE" | "BLOCKED"
    summary: str
    files: dict[str, str] = field(default_factory=dict)


def validate_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    p = PurePosixPath(path)
    if (
        not path
        or len(path) > MAX_PATH_LEN
        or p.is_absolute()
        or any(part in ("", ".", "..") for part in p.parts)
        or p.parts[0] == ".git"
        or path.endswith("/")
    ):
        raise AgentOutputError(f"Invalid file path: {path!r} (use a relative path inside the repository)")
    return str(p)


def _strip_fences(lines: list[str]) -> str:
    # trim blank lines around the block
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _FENCE_RE.match(lines[0]):
        fence = _FENCE_RE.match(lines[0]).group(1)
        closing = next(
            (i for i in range(len(lines) - 1, 0, -1) if lines[i].strip().startswith(fence[0] * 3)),
            None,
        )
        lines = lines[1:closing] if closing else lines[1:]
    text = "\n".join(lines)
    return text + "\n" if text and not text.endswith("\n") else text


def parse_agent_output(reply: str) -> AgentOutput:
    lines = (reply or "").replace("\r\n", "\n").split("\n")
    files: dict[str, str] = {}
    status: str | None = None
    status_rest = ""
    summary_lines: list[str] = []

    current_path: str | None = None
    current: list[str] = []
    fence: str | None = None  # open fence marker inside a FILE block
    section = "preamble"  # preamble | file | status

    def flush() -> None:
        nonlocal current_path, current, fence
        fence = None
        if current_path is not None:
            files[current_path] = _strip_fences(current)
        current_path, current = None, []

    for line in lines:
        if section == "file":
            m_fence = _FENCE_RE.match(line)
            if m_fence and fence is None:
                fence = m_fence.group(1)
            elif fence is not None and line.strip() and set(line.strip()) == {fence[0]} and len(line.strip()) >= len(fence):
                fence = None
                current.append(line)
                continue
        if fence is None:
            m_file = _FILE_RE.match(line)
            m_status = _STATUS_RE.match(line)
            if m_file:
                flush()
                current_path = validate_path(m_file.group(1))
                if current_path in files:
                    raise AgentOutputError(f"File {current_path} appears twice")
                section = "file"
                continue
            if m_status:
                flush()
                status = m_status.group(1).upper()
                status_rest = m_status.group(2).strip(" -—:*")
                section = "status"
                continue
        if section == "file":
            current.append(line)
        elif section == "status":
            m_sum = _SUMMARY_RE.match(line)
            summary_lines.append(m_sum.group(1) if m_sum else line)
    flush()

    if status is None:
        raise AgentOutputError("The reply has no 'STATUS: DONE' or 'STATUS: BLOCKED' line")
    if len(files) > MAX_FILES:
        raise AgentOutputError(f"Too many files ({len(files)} > {MAX_FILES}); split the work")
    if sum(len(c.encode()) for c in files.values()) > MAX_TOTAL_BYTES:
        raise AgentOutputError("Files are too large in total (> 1 MB)")
    empty = [p for p, c in files.items() if not c.strip()]
    if empty:
        raise AgentOutputError(f"Empty file content for: {', '.join(empty)}")
    if status == "DONE" and not files:
        raise AgentOutputError("STATUS is DONE but no FILE blocks were given")

    summary = "\n".join(summary_lines).strip() or status_rest
    return AgentOutput(status=status, summary=summary, files=files)
