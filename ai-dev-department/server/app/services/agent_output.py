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
_MESSAGE_RE = re.compile(r"^\s*(?:\*\*)?MESSAGE\s+to\s+([^:*]{1,60})(?:\*\*)?:(?:\*\*)?\s*(.+)$", re.IGNORECASE)
_QUESTION_RE = re.compile(r"^\s*(?:\*\*)?QUESTION:(?:\*\*)?\s*(.+)$", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^\s*(?:\*\*)?SUMMARY:(?:\*\*)?\s*(.*)$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


class AgentOutputError(ValueError):
    """The reply does not follow the output contract (message is shown to the agent)."""


@dataclass
class AgentOutput:
    status: str  # "DONE" | "BLOCKED"
    summary: str
    files: dict[str, str] = field(default_factory=dict)
    messages: list[tuple[str, str]] = field(default_factory=list)  # (to, text)
    questions: list[str] = field(default_factory=list)


def _norm_scope(entry: str) -> str:
    """Drop a leading "./" or "/" only - hidden dirs like ".github/" must survive."""
    entry = entry.strip()
    while entry.startswith("./"):
        entry = entry[2:]
    return entry.lstrip("/")


def in_scope(path: str, scope: list[str] | None) -> bool:
    """`scope` entries are directories ("src/api/") or exact files ("README.md")."""
    if not scope:
        return True
    for entry in scope:
        entry = _norm_scope(entry)
        if not entry:
            continue
        if entry.endswith("/") and path.startswith(entry):
            return True
        if path == entry or path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def check_scope(output: "AgentOutput", scope: list[str] | None) -> None:
    """Agents working in parallel must stay in their area (no conflicts)."""
    outside = [p for p in output.files if not in_scope(p, scope)]
    if outside:
        raise AgentOutputError(
            f"These files are outside your area {scope}: {', '.join(outside)}. "
            "Only write files inside your area; ask the owner with a MESSAGE line instead."
        )


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
    # A rendered code block may leave its language label right above the fence.
    if len(lines) >= 2 and not _FENCE_RE.match(lines[0]) and _FENCE_RE.match(lines[1]) \
            and re.fullmatch(r"[A-Za-z0-9+#.\-]{1,20}", lines[0].strip()):
        lines = lines[1:]
    # ...or render it as a separate block that holds only the label (```\nHTML\n```),
    # followed by the real code block: drop that label block.
    while (len(lines) >= 4 and _FENCE_RE.match(lines[0]) and _FENCE_RE.match(lines[2])
           and re.fullmatch(r"[A-Za-z][A-Za-z0-9+#. \-]{0,19}", lines[1].strip())
           and any(_FENCE_RE.match(x) for x in lines[3:])):
        lines = lines[3:]
        while lines and not lines[0].strip():
            lines.pop(0)
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
    messages: list[tuple[str, str]] = []
    questions: list[str] = []

    current_path: str | None = None
    current: list[str] = []
    fence: str | None = None  # open fence marker inside a FILE block
    block_closed = False  # the current FILE block's fenced content has ended
    section = "preamble"  # preamble | file | status

    def flush() -> None:
        nonlocal current_path, current, fence, block_closed
        fence = None
        block_closed = False
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
                block_closed = True
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
            if section != "file" or block_closed:
                m_msg = _MESSAGE_RE.match(line)
                m_q = _QUESTION_RE.match(line)
                if m_msg:
                    messages.append((m_msg.group(1).strip(), m_msg.group(2).strip()))
                    continue
                if m_q:
                    questions.append(m_q.group(1).strip())
                    continue
        if section == "file":
            current.append(line)
        elif section == "status":
            m_sum = _SUMMARY_RE.match(line)
            if m_sum:
                summary_lines.append(m_sum.group(1))
            elif summary_lines and not line.strip():
                section = "after"  # the summary paragraph ended: the rest is service UI («Deployed…», «Show more»)
            else:
                summary_lines.append(line)
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
    if status == "DONE":
        from app.services.file_checks import check_complete

        problems = [f"{p}: {r}" for p, c in files.items() if (r := check_complete(p, c))]
        if problems:
            raise AgentOutputError(
                "Some files look unfinished: " + "; ".join(problems[:5])
                + ". Send every file complete; split very large files into several smaller files."
            )

    summary = "\n".join(summary_lines).strip() or status_rest
    return AgentOutput(status=status, summary=summary, files=files,
                       messages=messages[:10], questions=questions[:5])
