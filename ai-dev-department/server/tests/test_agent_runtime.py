"""The agent loop, tools and skills, with a scripted model (no network, no keys)."""

from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from app.agents import skills as skills_mod
from app.agents.llm import LLMResponse
from app.agents.runtime import AgentSpec, run_agent
from app.agents.tools import ToolContext, run_tool


def _call(tool: str, n: int = 0, **args) -> dict:
    return {"type": "tool_use", "id": f"{tool}-{n}", "name": tool, "input": args}


class Script:
    """Returns the scripted replies in order; records what the model was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, provider, req):
        import dataclasses

        self.requests.append(dataclasses.replace(req, messages=list(req.messages)))  # a snapshot
        content = self.replies.pop(0)
        if isinstance(content, str):
            return LLMResponse([{"type": "text", "text": content}], "end", 10, 5)
        return LLMResponse(content, "tool_use", 10, 5)

    def results(self, i: int) -> list[dict]:
        """The tool results the model got back before its reply number i (0-based)."""
        return self.requests[i].messages[-1]["content"]


def _ctx(tmp_path: Path, **kw) -> tuple[ToolContext, list[dict]]:
    events: list[dict] = []

    async def emit(e):
        events.append(e)

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ToolContext(workspace=ws, role="Coder", emit=emit, sandbox="off", **kw), events


def test_an_agent_writes_edits_runs_and_reports(tmp_path):
    script = Script(
        [{"type": "text", "text": "Creating the file"}, _call("Write", path="src/app.py", content="print('hi')\n")],
        [_call("Edit", path="src/app.py", old_string="hi", new_string="hello")],
        [_call("Bash", command="python3 src/app.py")],
        "Done: src/app.py prints hello (checked with python3).",
    )
    ctx, events = _ctx(tmp_path)
    spec = AgentSpec(role="Coder", provider="openrouter", model="m")
    result = asyncio.run(run_agent(spec, ctx, "Make app.py print hello", script))
    assert (ctx.workspace / "src/app.py").read_text() == "print('hello')\n"
    assert script.results(3)[0]["content"].strip() == "hello"
    assert result.stopped == "done" and result.turns == 4 and "prints hello" in result.text
    assert result.tokens_in == 40
    kinds = [e["type"] for e in events]
    assert kinds.count("tool") == 3 and kinds.count("tool_done") == 3 and "file" in kinds
    assert "Coder" in script.requests[0].system and {t["name"] for t in script.requests[0].tools} >= {"Read", "Bash"}


def test_file_tools_never_leave_the_workspace(tmp_path):
    ctx, _ = _ctx(tmp_path)
    (tmp_path / "secret.txt").write_text("top secret")
    os.symlink(tmp_path / "secret.txt", ctx.workspace / "link.txt")

    async def go():
        return [await run_tool(ctx, "Write", {"path": "../evil.txt", "content": "x"}),
                await run_tool(ctx, "Read", {"path": "/etc/passwd"}),
                await run_tool(ctx, "Read", {"path": "link.txt"}),
                await run_tool(ctx, "Edit", {"path": "../secret.txt", "old_string": "top", "new_string": "no"})]

    for text, is_error in asyncio.run(go()):
        assert is_error and "outside the workspace" in text
    assert not (tmp_path / "evil.txt").exists() and (tmp_path / "secret.txt").read_text() == "top secret"


def test_edit_needs_an_exact_unique_match(tmp_path):
    ctx, _ = _ctx(tmp_path)
    (ctx.workspace / "a.txt").write_text("x = 1\nx = 1\n")

    async def go():
        return [await run_tool(ctx, "Edit", {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 2"}),
                await run_tool(ctx, "Edit", {"path": "a.txt", "old_string": "y", "new_string": "z"}),
                await run_tool(ctx, "Edit", {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 2",
                                             "replace_all": True})]

    (t1, e1), (t2, e2), (t3, e3) = asyncio.run(go())
    assert e1 and "2 times" in t1 and e2 and "not found" in t2 and not e3
    assert (ctx.workspace / "a.txt").read_text() == "x = 2\nx = 2\n"


def test_glob_grep_and_read_with_line_numbers(tmp_path):
    ctx, _ = _ctx(tmp_path)
    (ctx.workspace / "src").mkdir()
    (ctx.workspace / "src/a.py").write_text("def alpha():\n    return 1\n")
    (ctx.workspace / "node_modules").mkdir()
    (ctx.workspace / "node_modules/x.py").write_text("def alpha(): pass\n")

    async def go():
        return (await run_tool(ctx, "Glob", {"pattern": "**/*.py"}), await run_tool(ctx, "Grep", {"pattern": "def alph"}),
                await run_tool(ctx, "Read", {"path": "src/a.py", "offset": 2}))

    (g, _), (r, _), (read, _) = asyncio.run(go())
    assert g.strip() == "src/a.py" and r.strip() == "src/a.py:1: def alpha():"
    assert read.splitlines()[0].split("\t") == ["     2", "    return 1"]


def test_bash_has_a_time_limit_and_no_server_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("FERNET_KEY", "server-secret-value")
    monkeypatch.setenv("AIDD_SOME_TOKEN", "another-secret")
    ctx, _ = _ctx(tmp_path, bash_timeout=1.0)

    async def go():
        return (await run_tool(ctx, "Bash", {"command": "env; pwd"}),
                await run_tool(ctx, "Bash", {"command": "sleep 5; echo never"}),
                await run_tool(ctx, "Bash", {"command": "exit 3"}))

    (env_out, _), (slow, _), (failed, _) = asyncio.run(go())
    assert "server-secret-value" not in env_out and "another-secret" not in env_out
    assert str(ctx.workspace.resolve()) in env_out
    assert "timed out" in slow and "never" not in slow
    assert "[exit code 3]" in failed


def test_tool_calls_of_one_reply_run_in_parallel(tmp_path):
    script = Script([_call("Bash", 1, command="sleep 1; echo a"), _call("Bash", 2, command="sleep 1; echo b")], "ok")
    ctx, _ = _ctx(tmp_path)
    started = time.monotonic()
    asyncio.run(run_agent(AgentSpec(role="Coder", provider="groq", model="m"), ctx, "go", script))
    assert time.monotonic() - started < 1.8
    results = script.results(1)
    assert [r["content"].strip() for r in results] == ["a", "b"]  # both answers in ONE message, in order


def test_skills_are_listed_and_opened_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    skills_mod.install_text(1, "---\nname: house-style\ndescription: Our naming rules. Use for any new module.\n---\n"
                               "Name modules in snake_case.\n")
    ctx, events = _ctx(tmp_path)
    ctx.skills = skills_mod.skill_set(1, ["house-style", "code-review"])
    ctx.read_roots = ctx.skills.roots
    script = Script([_call("Skill", name="house-style")], [_call("Skill", 2, name="nope")], "ok")
    asyncio.run(run_agent(AgentSpec(role="Coder", provider="groq", model="m"), ctx, "go", script))
    system = script.requests[0].system
    assert "house-style: Our naming rules" in system and "code-review:" in system and "debugging" not in system
    assert "snake_case" in script.results(1)[0]["content"]
    assert script.results(2)[0]["is_error"] and "available: code-review, house-style" in script.results(2)[0]["content"]
    assert {"type": "skill", "name": "house-style"} in events


def test_skill_files_and_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    with pytest.raises(skills_mod.SkillError):
        skills_mod.install_text(1, "no front matter")
    with pytest.raises(skills_mod.SkillError):
        skills_mod.install_text(1, "---\nname: Bad Name\ndescription: x\n---\n")

    def zipped(files: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, text in files.items():
                zf.writestr(name, text)
        return buf.getvalue()

    skill = skills_mod.install_zip(1, zipped({"pdf-tools/SKILL.md": "---\nname: pdf-tools\ndescription: PDFs.\n---\nSee ref.md",
                                              "pdf-tools/ref.md": "details"}))
    assert (skill.path / "ref.md").read_text() == "details" and skill.body() == "See ref.md"
    with pytest.raises(skills_mod.SkillError):
        skills_mod.install_zip(1, zipped({"SKILL.md": "---\nname: evil\ndescription: x\n---\n", "../../escape.txt": "x"}))
    assert not (tmp_path / "data" / "escape.txt").exists()
    names = [s.name for s in skills_mod.all_skills(1)]
    assert "pdf-tools" in names and "code-review" in names
    assert skills_mod.remove(1, "pdf-tools") and "pdf-tools" not in [s.name for s in skills_mod.all_skills(1)]


def test_bad_tool_input_and_the_turn_limit(tmp_path):
    loop = [[_call("Read", i, path="nothing.txt")] for i in range(3)]
    script = Script([{"type": "tool_use", "id": "x", "name": "Bash", "input": {"__invalid_json__": "{oops"}}], *loop)
    ctx, _ = _ctx(tmp_path)
    result = asyncio.run(run_agent(AgentSpec(role="Coder", provider="groq", model="m", max_turns=4), ctx, "go", script))
    assert "not valid JSON" in script.results(1)[0]["content"]
    assert result.stopped == "turn_limit"


def test_a_tool_the_role_does_not_have_is_refused(tmp_path):
    script = Script([_call("Bash", command="rm -rf /")], "ok")
    ctx, _ = _ctx(tmp_path)
    asyncio.run(run_agent(AgentSpec(role="Reviewer", provider="groq", model="m", tools=["Read", "Grep"]), ctx, "go",
                          script))
    assert script.results(1)[0]["is_error"] and "not available" in script.results(1)[0]["content"]
    assert {t["name"] for t in script.requests[0].tools} == {"Read", "Grep"}


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="bubblewrap is not installed")
def test_bash_in_the_sandbox_cannot_see_server_secrets(tmp_path, monkeypatch):
    from app.agents import tools as tools_mod

    data = tmp_path / "data"
    ws = data / "workspaces" / "run-1"
    ws.mkdir(parents=True)
    (data / "app.db").write_text("the database")
    monkeypatch.setattr(tools_mod, "DATA_DIR", data)
    env_file = tools_mod.SERVER_ROOT / ".env"
    ctx = ToolContext(workspace=ws, role="Coder", sandbox="bwrap")
    cmd = (f"cat {data}/app.db 2>/dev/null | wc -c; ls {data}/workspaces | wc -l; "
           f"cat {env_file} 2>/dev/null | wc -c; echo made > out.txt; cat out.txt")
    out, is_error = asyncio.run(run_tool(ctx, "Bash", {"command": cmd}))
    lines = out.split()
    assert not is_error and lines[0] == "0"  # the database is hidden
    assert lines[1] == "1"  # only its own workspace is visible inside the data folder
    assert lines[2] == "0"  # the server's .env is hidden
    assert lines[3] == "made" and (ws / "out.txt").read_text() == "made\n"  # the workspace is writable


def test_a_call_cut_off_by_the_output_limit_is_not_run(tmp_path):
    """A huge Write cut off by the output limit (no path yet) is not run; the model is told to
    write the file in parts, and does."""
    cut = LLMResponse([_call("Write", content="<html>" + "x" * 50)], "max_tokens", 10, 16000)

    class CutScript(Script):
        async def __call__(self, provider, req):
            if not self.requests:
                self.requests.append(req)
                return cut
            return await super().__call__(provider, req)

    script = CutScript([_call("Write", 1, path="page.html", content="<html>")],
                       [_call("Bash", 2, command="echo '</html>' >> page.html")], "Done")
    ctx, events = _ctx(tmp_path)
    spec = AgentSpec(role="Coder", provider="custom", model="m", tools=["Write", "Bash"])
    result = asyncio.run(run_agent(spec, ctx, "Make a page", script))
    assert result.stopped == "done"
    assert "cut off" in script.results(1)[0]["content"] and script.results(1)[0]["is_error"]
    assert (ctx.workspace / "page.html").read_text() == "<html></html>\n"
    assert any(e["type"] == "tool_done" and not e["ok"] and "обрезан" in e["note"] for e in events)


def test_tools_take_the_argument_names_other_agents_use(tmp_path):
    """Models trained on Claude Code send file_path; a big file can be written in parts."""
    script = Script([_call("Write", path=None, file_path="a.html", content="<html>")],
                    [_call("Write", 1, file_path="a.html", content="</html>", append=True)],
                    [_call("Edit", 2, file_path="a.html", old_str="<html>", new_str="<html lang=ru>")], "Done")
    script.replies[0][0]["input"].pop("path")
    ctx, events = _ctx(tmp_path)
    spec = AgentSpec(role="Coder", provider="custom", model="m", tools=["Write", "Edit"])
    result = asyncio.run(run_agent(spec, ctx, "Make a page", script))
    assert result.stopped == "done"
    assert (ctx.workspace / "a.html").read_text() == "<html lang=ru></html>"
    assert "Appended" in script.results(2)[0]["content"]
    assert [e["arg"] for e in events if e["type"] == "tool"][0] == "a.html"
