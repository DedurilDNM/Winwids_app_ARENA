"""Claude Code as the engine of a chat turn.

One turn = one `claude -p --output-format stream-json` process in the chat's folder:
- the model calls go to this server's proxy (app/engine/proxy.py) with a per-run token, so
  the common key pool, rotation and limits work as before; no key reaches Claude Code;
- the process runs in the bwrap sandbox (only the chat folder is writable; the server's
  data, .env and the service user's home are hidden), with permission checks off inside it;
- HOME is the chat's .home: Claude Code keeps its session there, so the next message
  resumes the same conversation (--resume);
- skills: the user's and the built-in ones, linked into .home/.claude/skills (Claude Code
  finds them the usual way); its own bundled skills stay available;
- «Отдел» mode: the roles become Claude Code agents (--agents) on their own models;
- the «aidd» MCP server adds SendFile and Screenshot (app/engine/mcp_server.py);
- its stream of events becomes the chat's: steps, sub-agents, plan, text, tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.department import RunState
from app.agents.providers import PROVIDERS
from app.agents.runtime import AgentSpec, tool_detail
from app.agents.skills import all_skills
from app.agents.tools import describe_call, normalize_args
from app.config import DATA_DIR, SERVER_ROOT, get_settings
from app.engine import proxy

logger = logging.getLogger(__name__)

MCP_SERVER = Path(__file__).resolve().parent / "mcp_server.py"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AGENT_IDS = {"Architect": "architect", "Coder": "coder", "Reviewer": "reviewer", "Tester": "tester",
             "Integrator": "integrator"}
ROLE_BY_AGENT = {v: k for k, v in AGENT_IDS.items()}
AGENT_TEXT = {
    "Architect": "Designs the solution before code is written: structure, files, interfaces, data, trade-offs. "
                 "Use for new projects, big features and design questions.",
    "Coder": "Writes and changes code, pages and documents, runs them and fixes what fails. Use for implementation.",
    "Reviewer": "Reviews changes for bugs, security, edge cases and clarity; reports findings with file:line. "
                "Use after a change, before calling it done.",
    "Tester": "Writes and runs tests, reproduces bugs, checks pages with screenshots; reports what passed and "
              "what failed. Use to verify work.",
    "Integrator": "Wires parts together: builds, dependencies, configuration, git, deployment scripts.",
}
APPEND_PROMPT = """You work inside AIDD, a web app: the user writes to you in a chat and sees your steps there.
- Answer in the user's language.
- The user sees only what you send: show finished deliverables (a prototype page, a document, a spreadsheet, an
  image, a report, a screenshot) with the SendFile tool (mcp__aidd__SendFile). A page opens live in the chat.
- For a web page or prototype: look at it with the Screenshot tool (mcp__aidd__Screenshot) - desktop and phone -
  fix what looks wrong and look again before you call it done.
- Files the user attached are in uploads/.
- Web search: if the WebSearch tool is not available, find pages with WebFetch on a search page
  (https://html.duckduckgo.com/html/?q=<words>) and then WebFetch the results you need.
- Documents: .docx/.xlsx/.pptx/.pdf with Python (python-docx, openpyxl, python-pptx, reportlab, pypdf are
  installed); use the matching skill when there is one."""
DEPARTMENT_PROMPT = """You lead a department of agents ({names}). For a larger task, plan it (TodoWrite) and give
self-contained parts to them with the Agent tool (several at once when they are independent); check their
work. Do small things yourself."""
PLAN_PROMPT = "This turn is planning only: study the project and the request, then answer with a clear numbered plan."


# ------------------------------------------------------------------ install

_STATUS: dict = {}


def find_binary() -> str | None:
    s = get_settings()
    candidates = [s.ENGINE_CLAUDE_BIN, shutil.which("claude"), os.path.expanduser("~/.local/bin/claude"),
                  "/usr/local/bin/claude"]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


async def status(refresh: bool = False) -> dict:
    """{installed, path, version} - cached for a minute."""
    if _STATUS and not refresh and time.monotonic() - _STATUS.get("_at", 0) < 60:
        return {k: v for k, v in _STATUS.items() if not k.startswith("_")}
    path = find_binary()
    out = {"installed": False, "path": path, "version": None}
    if path:
        try:
            proc = await asyncio.create_subprocess_exec(path, "--version", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL,
                                                        env={"PATH": os.environ.get("PATH", ""), "HOME": "/tmp"})
            text, _ = await asyncio.wait_for(proc.communicate(), 20)
            out["version"] = text.decode().strip().split(" ")[0] or None
            out["installed"] = proc.returncode == 0
        except Exception as exc:
            out["error"] = str(exc)[:200]
    _STATUS.clear()
    _STATUS.update(out, _at=time.monotonic())
    return out


def anthropic_capable(spec: AgentSpec) -> bool:
    """Can this role's model be reached through the proxy (an Anthropic-format API)?"""
    return spec.provider in proxy.ANTHROPIC_BASES or spec.provider == "custom"


async def should_use(user_engine: str | None, lead: AgentSpec | None) -> bool:
    choice = (user_engine or get_settings().ENGINE or "auto").lower()
    if choice == "native" or lead is None:
        return False
    if not (await status())["installed"]:
        return False
    return choice == "claude_code" or anthropic_capable(lead)


# ------------------------------------------------------------------ one turn

@dataclass
class _Parse:
    """Claude Code's stream-json events -> the chat's events."""

    emit: object  # async (agent_id, event)
    lead_role: str
    lead_model: str
    session_id: str | None = None
    result: str = ""
    error: str | None = None
    tokens: list[int] = field(default_factory=lambda: [0, 0])
    tools: dict[str, tuple[str, str, dict]] = field(default_factory=dict)  # tool id -> (agent, name, args)
    agents: dict[str, str] = field(default_factory=dict)  # Agent tool_use id -> our agent id
    waiting: bool = False
    n: int = 0

    def agent_of(self, parent: str | None) -> str:
        if not parent:
            return "lead"
        if parent not in self.agents:
            self.n += 1
            self.agents[parent] = f"lead.{self.n}"
        return self.agents[parent]

    async def feed(self, ev: dict) -> None:
        kind, sub = ev.get("type"), ev.get("subtype")
        if kind == "system":
            if sub == "init":
                self.session_id = ev.get("session_id") or self.session_id
            elif sub == "status" and ev.get("status") == "requesting" and not self.waiting:
                self.waiting = True
                await self.emit("lead", {"type": "waiting", "role": self.lead_role, "model": self.lead_model})
            elif sub == "task_started" and ev.get("tool_use_id"):
                agent = self.agent_of(ev["tool_use_id"])
                kind_ = str(ev.get("subagent_type") or "agent")
                await self.emit(agent, {"type": "agent_start", "role": ROLE_BY_AGENT.get(kind_, kind_), "parent": "lead",
                                        "description": str(ev.get("description") or "")[:200], "model": ""})
            elif sub == "task_notification" and ev.get("tool_use_id"):
                st = str(ev.get("status") or "completed")
                await self.emit(self.agent_of(ev["tool_use_id"]), {
                    "type": "agent_done", "status": "done" if st == "completed" else "stopped" if st in ("stopped", "killed") else "failed",
                    "summary": str(ev.get("summary") or "")[:2000]})
            return
        if kind in ("assistant", "user"):
            self.waiting = False
            agent = self.agent_of(ev.get("parent_tool_use_id"))
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant":
                    await self._assistant_block(agent, block)
                elif block.get("type") == "tool_result":
                    await self._tool_result(block)
            return
        if kind == "result":
            self.waiting = False
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            for usage in (ev.get("modelUsage") or {}).values():
                self.tokens[0] += int(usage.get("inputTokens") or 0) + int(usage.get("cacheReadInputTokens") or 0) \
                    + int(usage.get("cacheCreationInputTokens") or 0)
                self.tokens[1] += int(usage.get("outputTokens") or 0)
            if ev.get("is_error"):
                self.error = str(ev.get("result") or ev.get("subtype") or "error")[:500]
            else:
                self.error = None
                self.result = str(ev.get("result") or self.result)

    async def _assistant_block(self, agent: str, block: dict) -> None:
        t = block.get("type")
        if t == "text" and (block.get("text") or "").strip():
            await self.emit(agent, {"type": "text", "text": block["text"][:2000]})
        elif t == "tool_use":
            name = str(block.get("name") or "")
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name in ("Agent", "Task"):
                self.agent_of(block.get("id"))  # its own block comes with task_started
                return
            if name == "TodoWrite":
                todos = [{"content": str(x.get("content") or x.get("activeForm") or "")[:300],
                          "status": x.get("status", "pending")} for x in args.get("todos") or [] if isinstance(x, dict)]
                await self.emit(agent, {"type": "todos", "todos": todos})
                return
            short = name.split("__")[-1] if name.startswith("mcp__") else name
            args = normalize_args(args)
            self.tools[block.get("id")] = (agent, short, args)
            await self.emit(agent, {"type": "tool", "tool": short, "arg": describe_call(short, args), "id": block.get("id")})

    async def _tool_result(self, block: dict) -> None:
        tool = self.tools.pop(block.get("tool_use_id"), None)
        if tool is None:
            return
        agent, name, args = tool
        body = block.get("content")
        if isinstance(body, list):
            body = "\n".join(str(p.get("text", "")) for p in body if isinstance(p, dict) and p.get("type") == "text")
        text, is_error = str(body or ""), bool(block.get("is_error"))
        detail = tool_detail(name, args, text, is_error) if name in ("Edit", "Write", "Bash", "Read") else {}
        await self.emit(agent, {"type": "tool_done", "tool": name, "id": block.get("tool_use_id"), "ok": not is_error,
                                "note": text[:200] if is_error else "", **detail})


def _sandbox(argv: list[str], ws: Path, binary: str, read_roots: list[Path]) -> list[str]:
    if not shutil.which("bwrap") or str(get_settings().AGENT_BASH_SANDBOX) == "off":
        return argv
    wsr = str(ws.resolve())
    out = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--tmpfs", str(DATA_DIR)]
    home = os.path.expanduser("~")
    if home and home != "/" and not wsr.startswith(home + "/"):
        out += ["--tmpfs", home]  # the service user's keys and configs stay hidden
    env_file = SERVER_ROOT / ".env"
    if env_file.exists():
        out += ["--ro-bind", "/dev/null", str(env_file)]
    real = str(Path(binary).resolve())
    hidden = [str(DATA_DIR)] + ([home] if home and home != "/" else [])
    if any(real.startswith(h + "/") for h in hidden):  # e.g. ~/.local/share/claude/versions/<v>
        out += ["--ro-bind", real, real]
    out += ["--bind", wsr, wsr]
    for root in read_roots:
        path = str(Path(root).resolve())
        if path.startswith(str(DATA_DIR)) and Path(path).is_dir():
            out += ["--ro-bind", path, path]
    return out + ["--chdir", wsr, "--die-with-parent", "--", real] + argv[1:]


def _link_skills(home: Path, user_id: int, names: list[str] | None) -> list[Path]:
    target = home / ".claude" / "skills"
    if target.exists():
        for item in target.iterdir():
            if item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    roots = []
    for skill in all_skills(user_id):
        if names is not None and skill.name not in names:
            continue
        (target / skill.name).symlink_to(skill.path.resolve(), target_is_directory=True)
        roots.append(skill.path)
    return roots


def _agents(specs: dict[str, AgentSpec], lead_role: str) -> dict:
    out = {}
    for role, spec in specs.items():
        if role == lead_role or role not in AGENT_IDS:
            continue
        prompt = f"You are the {role} of a software department. {AGENT_TEXT[role]}"
        if spec.instructions:
            prompt += "\n\n" + spec.instructions
        out[AGENT_IDS[role]] = {"description": AGENT_TEXT[role], "prompt": prompt, "model": spec.model}
    return out


async def run_turn(st: RunState, lead_role: str, lead: AgentSpec, prompt: str, *, mode: str, chat=None,
                   history: list[dict] | None = None) -> str:
    """Run one turn with Claude Code; returns the final text. Raises on failure."""
    binary = find_binary()
    if binary is None:
        raise RuntimeError("Claude Code is not installed on the server")
    s = get_settings()
    ws = st.workspace
    home = ws / ".home"
    home.mkdir(parents=True, exist_ok=True)
    roots = _link_skills(home, st.user_id, lead.skills)

    specs = {r: sp for r, sp in st.specs.items() if anthropic_capable(sp)}
    models = {lead.model: lead.provider}
    for sp in specs.values():
        models.setdefault(sp.model, sp.provider)
    fallbacks = [f for f in lead.fallbacks if f.get("provider") in PROVIDERS and anthropic_capable(
        AgentSpec(role="x", provider=f["provider"], model=f["model"]))]
    for f in fallbacks:
        models.setdefault(f["model"], f["provider"])
    by_role = {r: sp.model for r, sp in specs.items()}
    small = by_role.get("Tester") or by_role.get("Reviewer") or lead.model

    async def emit(agent_id: str, event: dict) -> None:
        await st.event(agent_id, event)

    access = proxy.RunAccess(user_id=st.user_id, run_id=st.run_id, models=models, default_provider=lead.provider,
                             workspace=ws, emit=emit, should_continue=st.alive)
    token = proxy.open_access(access)
    base = f"{s.ENGINE_CALLBACK_URL.rstrip('/')}/api/llm-proxy/{token}"
    mcp = {"mcpServers": {"aidd": {"command": sys.executable, "args": ["-I", str(MCP_SERVER)],
                                   "env": {"AIDD_URL": base}}}}
    append = APPEND_PROMPT
    agents = {}
    if mode == "department":
        agents = _agents(specs, lead_role)
        if agents:
            append += "\n\n" + DEPARTMENT_PROMPT.format(names=", ".join(agents))
    if lead.instructions:
        append += "\n\n" + lead.instructions

    session = getattr(chat, "engine_session", None) if chat is not None else None
    resume = bool(session) and any((home / ".claude" / "projects").glob(f"*/{session}.jsonl"))
    if not resume:
        session = str(uuid.uuid4())
        if history:  # the chat started on the built-in engine: bring its earlier turns along
            past = "\n\n".join(f"{'User' if m['role'] == 'user' else 'You'}: {m['content'] if isinstance(m['content'], str) else ''}"
                               for m in history)[-20000:]
            prompt = f"Earlier in this chat:\n\n{past}\n\n---\n\n{prompt}"

    argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "plan" if mode == "plan" else "bypassPermissions",
            "--model", lead.model, "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
            "--append-system-prompt", append + ("\n\n" + PLAN_PROMPT if mode == "plan" else "")]
    argv += ["--resume", session] if resume else ["--session-id", session]
    if lead.effort in EFFORTS:
        argv += ["--effort", lead.effort]
    if int(s.ENGINE_AUTOCOMPACT_TOKENS or 0) >= 100000:  # the CLI takes 100k at least
        argv += ["--autocompact", str(int(s.ENGINE_AUTOCOMPACT_TOKENS))]
    if lead.provider != "anthropic":
        # web search is a server tool run by Anthropic's own API: an aggregator cannot run it, so every
        # call would fail; pages are read with WebFetch (it runs here)
        argv += ["--disallowedTools", "WebSearch"]
    if fallbacks:
        argv += ["--fallback-model", ",".join(f["model"] for f in fallbacks[:3])]
    if agents:
        argv += ["--agents", json.dumps(agents, ensure_ascii=False)]
    argv.append(prompt)

    env = {"PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
           "LANG": os.environ.get("LANG", "C.UTF-8"), "HOME": str(home), "TERM": "dumb", "NO_COLOR": "1",
           "IS_SANDBOX": "1", "CI": "1",
           "ANTHROPIC_BASE_URL": base, "ANTHROPIC_API_KEY": "aidd-engine",
           "ANTHROPIC_MODEL": lead.model, "ANTHROPIC_DEFAULT_OPUS_MODEL": lead.model,
           "ANTHROPIC_DEFAULT_SONNET_MODEL": by_role.get("Coder") or lead.model,
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": small, "ANTHROPIC_SMALL_FAST_MODEL": small,
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
           "DISABLE_ERROR_REPORTING": "1", "ENABLE_TOOL_SEARCH": "false", "API_TIMEOUT_MS": "600000"}
    if s.ENGINE_DISABLE_BETAS:
        env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

    parse = _Parse(emit, lead_role, lead.model, session_id=session)
    await emit("lead", {"type": "agent_start", "role": lead_role, "parent": None, "description": "Claude Code",
                        "provider": lead.provider, "model": lead.model})
    proc = await asyncio.create_subprocess_exec(
        *_sandbox(argv, ws, binary, roots), cwd=str(ws), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=64 * 1024 * 1024)
    stderr: list[bytes] = []

    async def read_err():
        async for line in proc.stderr:
            if len(stderr) < 200:
                stderr.append(line)

    async def watch():  # the user pressed «stop» or the turn ran too long
        deadline = time.monotonic() + 60 * float(s.ENGINE_MAX_MINUTES)
        while proc.returncode is None:
            await asyncio.sleep(1.0)
            if not await st.alive() or time.monotonic() > deadline:
                _kill(proc)
                return

    err_task, watch_task = asyncio.create_task(read_err()), asyncio.create_task(watch())
    try:
        async for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            await parse.feed(ev)
        await proc.wait()
    except asyncio.CancelledError:
        _kill(proc)
        raise
    finally:
        watch_task.cancel()
        await asyncio.gather(err_task, return_exceptions=True)
        proxy.close_access(token)
        st.counters["tokens_in"] += max(parse.tokens[0], access.tokens[0])
        st.counters["tokens_out"] += max(parse.tokens[1], access.tokens[1])
        if chat is not None and parse.session_id:
            await _save_session(chat.id, parse.session_id)
    if not await st.alive():
        await emit("lead", {"type": "agent_done", "status": "stopped"})
        raise asyncio.CancelledError("stopped")
    if parse.error or (proc.returncode and not parse.result):
        why = parse.error or b"".join(stderr).decode("utf-8", "replace").strip()[-500:] or f"exit {proc.returncode}"
        await emit("lead", {"type": "agent_done", "status": "failed", "error": why[:300]})
        raise RuntimeError(f"Claude Code: {why}")
    await emit("lead", {"type": "agent_done", "status": "done", "summary": parse.result[:2000]})
    return parse.result


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


async def _save_session(chat_id: int, session_id: str) -> None:
    from app.db import SessionLocal
    from app.models import Chat

    async with SessionLocal() as db:
        chat = await db.get(Chat, chat_id)
        if chat is not None and chat.engine_session != session_id:
            chat.engine_session = session_id
            await db.commit()
