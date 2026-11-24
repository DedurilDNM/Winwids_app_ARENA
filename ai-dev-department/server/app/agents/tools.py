"""Agent tools, like Claude Code's: Read, Write, Edit, Glob, Grep, Bash, WebFetch,
TodoWrite, Skill, Task. Every file tool stays inside the run's workspace (skill
folders are readable too); Bash runs in the workspace with a time limit, a capped
output and an environment without the server's secrets (inside `bwrap` when the
server has it).
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import json
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import DATA_DIR, SERVER_ROOT

MAX_READ_LINES = 2000
MAX_LINE = 2000
MAX_OUTPUT = 30_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache"}


class ToolError(Exception):
    """A tool failed in a way the agent should see (returned as an error result)."""


@dataclass
class ToolContext:
    workspace: Path
    role: str = ""
    agent_id: str = ""
    read_roots: list[Path] = field(default_factory=list)  # extra read-only places (skills)
    skills: Any = None  # SkillSet for this agent
    todos: list[dict] = field(default_factory=list)
    # async (role, description, prompt) -> str: runs a sub-agent (Task); None = no sub-agents
    spawn: Callable[..., Awaitable[str]] | None = None
    # () -> str: reserves the next sub-agent id, called in the order of the Task calls
    reserve: Callable[[], str] | None = None
    emit: Callable[[dict], Awaitable[None]] | None = None
    bash_timeout: float = 120.0
    sandbox: str = "auto"  # auto | bwrap | off


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[ToolContext, dict], Awaitable[str]]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ------------------------------------------------------------------ paths

def resolve(ctx: ToolContext, path: str, *, write: bool = False) -> Path:
    if not path or not isinstance(path, str):
        raise ToolError("path is required (if the call was long, it may have been cut off by the output limit: "
                        "write big files in parts)")
    raw = Path(path)
    root = ctx.workspace.resolve()
    candidate = raw if raw.is_absolute() else root / raw
    real = candidate.resolve()
    if real == root or root in real.parents:
        return real
    if not write:
        for extra in ctx.read_roots:
            extra = extra.resolve()
            if real == extra or extra in real.parents:
                return real
    raise ToolError(f"{path}: outside the workspace (use paths inside the project folder)")


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.workspace.resolve()))
    except ValueError:
        return str(p)


def _cap(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-limit // 3:]
    return f"{head}\n…[{len(text) - len(head) - len(tail)} characters cut]…\n{tail}"


# ------------------------------------------------------------------ file tools

async def _read(ctx: ToolContext, a: dict) -> str:
    p = resolve(ctx, a.get("path", ""))
    if p.is_dir():
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))[:500]
        return "\n".join(f"{x.name}/" if x.is_dir() else x.name for x in items) or "(empty folder)"
    if not p.exists():
        raise ToolError(f"{a.get('path')}: no such file")
    data = p.read_bytes()
    if b"\0" in data[:8000]:
        return f"(binary file, {len(data)} bytes)"
    lines = data.decode("utf-8", "replace").splitlines()
    offset = max(1, int(a.get("offset") or 1))
    limit = min(MAX_READ_LINES, max(1, int(a.get("limit") or MAX_READ_LINES)))
    chunk = lines[offset - 1: offset - 1 + limit]
    out = "\n".join(f"{i:6}\t{line[:MAX_LINE]}" for i, line in enumerate(chunk, start=offset))
    if offset - 1 + limit < len(lines):
        out += f"\n… {len(lines) - (offset - 1 + limit)} more lines (use offset)"
    return out or "(empty file)"


async def _write(ctx: ToolContext, a: dict) -> str:
    p = resolve(ctx, a.get("path", ""), write=True)
    content = a.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be text")
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if a.get("append") and existed:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    else:
        p.write_text(content, encoding="utf-8")
    if ctx.emit:
        await ctx.emit({"type": "file", "path": _rel(ctx, p), "action": "updated" if existed else "created"})
    verb = "Appended to" if a.get("append") and existed else "Updated" if existed else "Created"
    return f"{verb} {_rel(ctx, p)} (now {p.stat().st_size} bytes)"


async def _edit(ctx: ToolContext, a: dict) -> str:
    p = resolve(ctx, a.get("path", ""), write=True)
    if not p.is_file():
        raise ToolError(f"{a.get('path')}: no such file (use Write to create it)")
    old, new = a.get("old_string"), a.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        raise ToolError("old_string (non-empty) and new_string are required")
    if old == new:
        raise ToolError("old_string and new_string are the same")
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        raise ToolError("old_string not found: Read the file and copy the text exactly")
    if count > 1 and not a.get("replace_all"):
        raise ToolError(f"old_string occurs {count} times: add context to make it unique, or set replace_all")
    text = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    if ctx.emit:
        await ctx.emit({"type": "file", "path": _rel(ctx, p), "action": "edited"})
    return f"Edited {_rel(ctx, p)} ({count if a.get('replace_all') else 1} replacement(s))"


def _walk(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            yield Path(root) / f


async def _glob(ctx: ToolContext, a: dict) -> str:
    base = resolve(ctx, a.get("path") or ".")
    pattern = a.get("pattern") or "**/*"
    matches = [p for p in base.glob(pattern) if p.is_file() and not (set(p.relative_to(base).parts) & SKIP_DIRS)]
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    shown = matches[:200]
    out = "\n".join(_rel(ctx, p) for p in shown)
    return (out + (f"\n… {len(matches) - 200} more" if len(matches) > 200 else "")) or "(no matches)"


async def _grep(ctx: ToolContext, a: dict) -> str:
    base = resolve(ctx, a.get("path") or ".")
    try:
        rx = re.compile(a.get("pattern") or "", re.IGNORECASE if a.get("ignore_case") else 0)
    except re.error as exc:
        raise ToolError(f"bad regular expression: {exc}") from None
    glob = a.get("glob")
    limit = min(500, int(a.get("max_results") or 200))
    out: list[str] = []
    files = [base] if base.is_file() else _walk(base)
    for f in files:
        if glob and not fnmatch.fnmatch(f.name, glob) and not fnmatch.fnmatch(_rel(ctx, f), glob):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4000]:
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(line):
                out.append(f"{_rel(ctx, f)}:{n}: {line.strip()[:300]}")
                if len(out) >= limit:
                    return "\n".join(out) + "\n… (more matches; narrow the search)"
    return "\n".join(out) or "(no matches)"


# ------------------------------------------------------------------ Bash

_SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "TERM")


def _bash_argv(ctx: ToolContext, command: str) -> list[str]:
    ws = str(ctx.workspace.resolve())
    use_bwrap = ctx.sandbox in ("auto", "bwrap") and shutil.which("bwrap")
    if ctx.sandbox == "bwrap" and not use_bwrap:
        raise ToolError("the bwrap sandbox is required but not installed on the server")
    if not use_bwrap:
        return ["/bin/bash", "-c", command]
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--tmpfs", str(DATA_DIR)]
    home = os.path.expanduser("~")
    if home and home != "/" and not ws.startswith(home + "/"):
        argv += ["--tmpfs", home]  # the server user's keys and configs stay hidden
    env_file = SERVER_ROOT / ".env"
    if env_file.exists():
        argv += ["--ro-bind", "/dev/null", str(env_file)]
    argv += ["--bind", ws, ws]
    for root in ctx.read_roots:  # the skills' own scripts and references, read-only
        path = str(Path(root).resolve())
        if path.startswith(str(DATA_DIR)) and Path(path).is_dir():
            argv += ["--ro-bind", path, path]
    argv += ["--chdir", ws, "--unshare-pid", "--die-with-parent", "/bin/bash", "-c", command]
    return argv


async def _bash(ctx: ToolContext, a: dict) -> str:
    command = a.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command is required")
    timeout = min(600.0, max(1.0, float(a.get("timeout") or ctx.bash_timeout)))
    env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
    import sys

    tools_bin = str(Path(sys.executable).parent)  # python3 / pip with python-docx, openpyxl, python-pptx ...
    env["PATH"] = f"{tools_bin}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    home = ctx.workspace / ".home"
    home.mkdir(exist_ok=True)
    env.update({"HOME": str(home), "CI": "1", "NO_COLOR": "1"})
    proc = await asyncio.create_subprocess_exec(
        *_bash_argv(ctx, command), cwd=str(ctx.workspace), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code = proc.returncode
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, code = (await proc.communicate())[0] if proc.stdout else b"", None
        text = _cap((out or b"").decode("utf-8", "replace"))
        return f"{text}\n[timed out after {timeout:.0f}s; the command was stopped]"
    except asyncio.CancelledError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        raise
    text = _cap((out or b"").decode("utf-8", "replace"))
    return f"{text}\n[exit code {code}]" if code else (text or "(no output)")


# ------------------------------------------------------------------ web, todos, skills, sub-agents

def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


async def _webfetch(ctx: ToolContext, a: dict) -> str:
    import httpx

    url = a.get("url") or ""
    if not re.match(r"^https?://", url):
        raise ToolError("url must start with http:// or https://")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; AIDD-agent)"}) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise ToolError(f"could not fetch: {type(exc).__name__}") from None
    body = resp.text[:2_000_000]
    text = _html_to_text(body) if "html" in resp.headers.get("content-type", "") else body
    return _cap(f"[{resp.status_code}] {resp.url}\n\n{text}", 50_000)


async def _todo(ctx: ToolContext, a: dict) -> str:
    todos = a.get("todos")
    if not isinstance(todos, list):
        raise ToolError("todos must be a list")
    clean = [{"content": str(t.get("content", ""))[:300], "status": t.get("status", "pending")}
             for t in todos if isinstance(t, dict)]
    ctx.todos[:] = clean
    if ctx.emit:
        await ctx.emit({"type": "todos", "todos": clean})
    done = sum(1 for t in clean if t["status"] == "completed")
    return f"Plan updated: {done}/{len(clean)} done"


async def _skill(ctx: ToolContext, a: dict) -> str:
    if ctx.skills is None:
        raise ToolError("no skills are available to this agent")
    skill = ctx.skills.get(a.get("name") or "")
    if skill is None:
        names = ", ".join(s.name for s in ctx.skills.items) or "none"
        raise ToolError(f"no skill named {a.get('name')!r}; available: {names}")
    files = [str(p.relative_to(skill.path)) for p in sorted(skill.path.rglob("*")) if p.is_file()
             and p.name != "SKILL.md"][:50]
    if ctx.emit:
        await ctx.emit({"type": "skill", "name": skill.name})
    extra = (f"\n\nFiles of this skill (read them with Read when the instructions say so), in {skill.path}:\n"
             + "\n".join(files)) if files else ""
    return f"# Skill: {skill.name}\n\n{skill.body()}{extra}"


_KINDS = {".html": "html", ".htm": "html", ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
          ".webp": "image", ".svg": "image", ".pdf": "pdf", ".xlsx": "sheet", ".xlsm": "sheet", ".csv": "sheet",
          ".docx": "document", ".pptx": "slides", ".md": "text", ".txt": "text", ".json": "text"}


async def _sendfile(ctx: ToolContext, a: dict) -> str:
    p = resolve(ctx, a.get("path", ""), write=True)  # only files of the project
    if not p.is_file():
        raise ToolError(f"{a.get('path')}: no such file (create it first)")
    size = p.stat().st_size
    if size > 50_000_000:
        raise ToolError("the file is larger than 50 MB")
    kind = _KINDS.get(p.suffix.lower(), "file")
    if ctx.emit:
        await ctx.emit({"type": "attachment", "path": _rel(ctx, p), "kind": kind, "size": size,
                        "caption": str(a.get("caption") or "")[:300]})
    return f"Shown to the user in the chat: {_rel(ctx, p)} ({kind}, {size} bytes)"


async def _task(ctx: ToolContext, a: dict) -> str:
    if ctx.spawn is None:
        raise ToolError("this agent cannot start sub-agents")
    role, prompt = a.get("role") or "", a.get("prompt") or ""
    if not role or not prompt:
        raise ToolError("role and prompt are required")
    return await ctx.spawn(role, str(a.get("description") or "")[:120], prompt, a.get("__child_id"))


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("Read", "Read a file of the project (numbered lines) or list a folder. Paths are relative to the project "
         "root. Use offset/limit for long files.",
         _schema({"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
         _read),
    Tool("Write", "Create or overwrite a file (path first, then content). For a big file (over ~300 lines) "
         "write it in parts: the first part, then the next ones with append: true. Prefer Edit for changes "
         "to an existing file.",
         _schema({"path": {"type": "string"}, "content": {"type": "string"},
                  "append": {"type": "boolean", "description": "add to the end of the file instead of replacing it"}},
                 ["path", "content"]), _write),
    Tool("Edit", "Replace an exact piece of text in a file. old_string must match exactly once (or set replace_all). "
         "Read the file first.",
         _schema({"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
                  "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]), _edit),
    Tool("Glob", "Find files by a glob pattern such as **/*.py (newest first).",
         _schema({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]), _glob),
    Tool("Grep", "Search file contents with a regular expression; returns file:line: text.",
         _schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
                  "ignore_case": {"type": "boolean"}, "max_results": {"type": "integer"}}, ["pattern"]), _grep),
    Tool("Bash", "Run a shell command in the project folder (build, tests, git, package managers). It has a time "
         "limit; output is shortened when long. No interactive commands.",
         _schema({"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), _bash),
    Tool("WebFetch", "Download a web page or file by URL and return its text.",
         _schema({"url": {"type": "string"}}, ["url"]), _webfetch),
    Tool("TodoWrite", "Keep your plan as a checklist: send the whole list each time "
         "(status: pending | in_progress | completed).",
         _schema({"todos": {"type": "array", "items": {"type": "object", "properties": {
             "content": {"type": "string"}, "status": {"type": "string",
                                                       "enum": ["pending", "in_progress", "completed"]}},
             "required": ["content", "status"], "additionalProperties": False}}}, ["todos"]), _todo),
    Tool("Skill", "Open a skill by name: returns its instructions. Use it when a task matches a skill's description.",
         _schema({"name": {"type": "string"}}, ["name"]), _skill),
    Tool("SendFile", "Show a finished file to the user in the chat: an HTML prototype opens as a live page, images "
         "and PDF are shown, spreadsheets as a table, any file can be downloaded. Use it for deliverables "
         "(prototypes, documents, reports), not for every file you touch.",
         _schema({"path": {"type": "string"}, "caption": {"type": "string"}}, ["path"]), _sendfile),
    Tool("Task", "Give a piece of work to a sub-agent of another role and get its final report. Several Task calls "
         "in one reply run in parallel. Give each a complete, self-contained prompt: it does not see this chat.",
         _schema({"role": {"type": "string"}, "description": {"type": "string"}, "prompt": {"type": "string"}},
                 ["role", "prompt"]), _task),
)}

ALL_TOOL_NAMES = list(TOOLS)


# names other agents' tools use for the same argument (Claude Code: file_path, ...)
_ALIASES = {"path": ("file_path", "filepath", "filename", "file", "notebook_path", "target_file"),
            "content": ("contents", "text", "file_text"), "old_string": ("old_str", "old_text"),
            "new_string": ("new_str", "new_text"), "command": ("cmd",)}


def normalize_args(args: dict) -> dict:
    args = dict(args)
    for name, aliases in _ALIASES.items():
        if name not in args:
            for alias in aliases:
                if alias in args:
                    args[name] = args.pop(alias)
                    break
    return args


async def run_tool(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
    """(result text, is_error)"""
    tool = TOOLS.get(name)
    if tool is None:
        return f"Unknown tool {name!r}", True
    if not isinstance(args, dict) or "__invalid_json__" in args:
        return "The tool input was not valid JSON; send the arguments again as a JSON object", True
    args = normalize_args(args)
    try:
        return await tool.run(ctx, args), False
    except ToolError as exc:
        return str(exc), True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a tool bug must not kill the agent
        return f"{name} failed: {type(exc).__name__}: {str(exc)[:300]}", True


def describe_call(name: str, args: dict) -> str:
    """A short line for the UI (paths and commands only, shortened)."""
    if not isinstance(args, dict):
        return ""
    args = {k: v for k, v in args.items() if not k.startswith("__")}
    for key in ("path", "pattern", "command", "url", "name", "role"):
        if args.get(key):
            value = str(args[key]).replace("\n", " ")
            return value[:160]
    if name == "TodoWrite":
        return f"{len(args.get('todos') or [])} пунктов"
    return json.dumps(args, ensure_ascii=False)[:160]
