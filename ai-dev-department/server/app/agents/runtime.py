"""The agent loop: model -> tools -> model ... until the model answers without tools.

Tool calls of one reply run concurrently (several Task calls = parallel sub-agents)
and their results go back in ONE message. History is append-only for Claude models
(their thinking blocks are replayed unchanged); for other providers very old tool
results are shortened once the conversation gets long.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.agents.llm import LLMResponse, Request
from app.agents.tools import TOOLS, ToolContext, describe_call, run_tool

logger = logging.getLogger(__name__)

KEEP_FULL_RESULTS = 8  # the newest tool results always stay whole
TRIM_TO = 1500
TRIM_AFTER_CHARS = 400_000


@dataclass
class AgentSpec:
    role: str
    provider: str
    model: str
    effort: str | None = None
    tools: list[str] = field(default_factory=lambda: list(TOOLS))
    skills: list[str] | None = None  # None = every skill
    instructions: str = ""  # the role's own prompt
    max_turns: int = 60
    max_tokens: int = 16000


@dataclass
class AgentResult:
    text: str
    turns: int
    tokens_in: int
    tokens_out: int
    stopped: str  # "done" | "turn_limit" | "refusal"


ROLE_DEFAULTS = {
    "Orchestrator": "You lead a department of AI agents. Break the job into well-defined pieces, give them to "
                    "sub-agents of the right roles with the Task tool (in parallel when they do not depend on each "
                    "other), check what comes back, and finish with a short report of what was done.",
    "Architect": "You design the structure: modules, data, interfaces. Write the design down in the project "
                 "(e.g. docs/ARCHITECTURE.md) so the other agents can follow it.",
    "Coder": "You implement features in the project's code, run the build and the tests, and fix what breaks.",
    "Reviewer": "You review changes for bugs, security problems and missing tests, and report concrete findings.",
    "Tester": "You write and run automated tests and report exactly what passes and what fails.",
    "Integrator": "You bring the pieces together: dependencies, configuration, build, a working whole.",
}


def system_prompt(spec: AgentSpec, ctx: ToolContext) -> str:
    parts = [
        f"You are the {spec.role} in a team of AI agents working on one software project.",
        spec.instructions or ROLE_DEFAULTS.get(spec.role, ""),
        "The project folder is your working directory; file paths are relative to it. You can only change files "
        "there. Use the tools to look at the real files before you change them, and verify your work (build, "
        "tests) when the project has a way to do it.",
        "When you are done, answer without calling tools: a short report - what you did, which files changed, "
        "how you verified it, and anything left open. Be concrete.",
    ]
    if ctx.skills is not None and ctx.skills.items and "Skill" in spec.tools:
        parts.append("Skills you can open with the Skill tool when a task matches the description:\n"
                     + ctx.skills.listing())
    if "Task" in spec.tools and ctx.spawn is not None:
        roles = getattr(ctx, "available_roles", None)
        if roles:
            parts.append("Roles you can give work to with the Task tool: " + ", ".join(roles) + ".")
    return "\n\n".join(p for p in parts if p)


def _trim(messages: list[dict]) -> None:
    size = sum(len(str(m.get("content"))) for m in messages)
    if size < TRIM_AFTER_CHARS:
        return
    results = [(i, j) for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], list)
               for j, b in enumerate(m["content"]) if b.get("type") == "tool_result"]
    for i, j in results[:-KEEP_FULL_RESULTS]:
        block = messages[i]["content"][j]
        body = block.get("content")
        if isinstance(body, str) and len(body) > TRIM_TO:
            block["content"] = body[:TRIM_TO] + f"\n…[shortened: {len(body) - TRIM_TO} characters of an old result]"


Complete = Callable[[str, Request], Awaitable[LLMResponse]]


async def run_agent(spec: AgentSpec, ctx: ToolContext, prompt: str, complete: Complete, *,
                    should_continue: Callable[[], Awaitable[bool]] | None = None) -> AgentResult:
    """`complete(provider, request)` performs one model call (through the key pool)."""
    tool_specs = [TOOLS[name].spec() for name in spec.tools if name in TOOLS
                  and not (name == "Task" and ctx.spawn is None)]
    system = system_prompt(spec, ctx)
    messages: list[dict] = [{"role": "user", "content": prompt}]
    tokens_in = tokens_out = 0
    nudges = 0
    last_text = ""

    async def emit(event: dict) -> None:
        if ctx.emit is not None:
            try:
                await ctx.emit(event)
            except Exception:
                logger.debug("agent event failed", exc_info=True)

    for turn in range(1, spec.max_turns + 1):
        if should_continue is not None and not await should_continue():
            raise asyncio.CancelledError("stopped")
        if spec.provider != "anthropic":
            _trim(messages)
        resp = await complete(spec.provider, Request(model=spec.model, messages=messages, system=system,
                                                     tools=tool_specs, max_tokens=spec.max_tokens, effort=spec.effort))
        tokens_in += resp.tokens_in
        tokens_out += resp.tokens_out
        messages.append({"role": "assistant", "content": resp.content, "_raw": resp.raw})
        await emit({"type": "usage", "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out})
        if resp.text:
            last_text = resp.text
            await emit({"type": "text", "text": resp.text[:2000]})
        calls = resp.tool_calls
        if resp.stop == "refusal":
            return AgentResult(last_text or "(the model declined this request)", turn, tokens_in, tokens_out, "refusal")
        if not calls:
            if resp.stop == "max_tokens" and nudges < 2:  # cut off mid-answer: let it go on
                nudges += 1
                messages.append({"role": "user", "content": "Continue."})
                continue
            return AgentResult(last_text, turn, tokens_in, tokens_out, "done")

        async def one(call: dict) -> dict:
            name, args = call["name"], call.get("input") or {}
            await emit({"type": "tool", "tool": name, "arg": describe_call(name, args), "id": call["id"]})
            if name not in spec.tools:
                text, is_error = f"The tool {name} is not available to the {spec.role}", True
            else:
                text, is_error = await run_tool(ctx, name, args)
            await emit({"type": "tool_done", "tool": name, "id": call["id"], "ok": not is_error,
                        "note": text[:200] if is_error else ""})
            return {"type": "tool_result", "tool_use_id": call["id"], "content": text, "is_error": is_error}

        results = await asyncio.gather(*(one(c) for c in calls))
        messages.append({"role": "user", "content": list(results)})
    return AgentResult(last_text or "(stopped: the turn limit was reached)", spec.max_turns, tokens_in, tokens_out,
                       "turn_limit")
