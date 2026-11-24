"""Files in the chat (SendFile, attachments, spreadsheet preview), document skills,
skills from a GitHub link, skill scripts inside the sandbox."""

from __future__ import annotations

import asyncio
import io
import shutil
import time
import zipfile

import httpx
import pytest

from app.agents import department, llm, skills as skills_mod
from app.agents.llm import LLMResponse
from app.agents.tools import ToolContext, run_tool


def _ctx(tmp_path, **kw):
    events = []

    async def emit(e):
        events.append(e)

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ToolContext(workspace=ws, emit=emit, sandbox="off", **kw), events


def test_sendfile_shows_a_project_file_in_the_chat(tmp_path):
    ctx, events = _ctx(tmp_path)
    (ctx.workspace / "proto").mkdir()
    (ctx.workspace / "proto/index.html").write_text("<h1>hi</h1>")
    (tmp_path / "secret.txt").write_text("x")

    async def go():
        return (await run_tool(ctx, "SendFile", {"path": "proto/index.html", "caption": "the main screen"}),
                await run_tool(ctx, "SendFile", {"path": "nope.pdf"}),
                await run_tool(ctx, "SendFile", {"path": "../secret.txt"}))

    (ok, e1), (missing, e2), (outside, e3) = asyncio.run(go())
    assert not e1 and "proto/index.html" in ok
    assert events == [{"type": "attachment", "path": "proto/index.html", "kind": "html", "size": 11,
                       "caption": "the main screen"}]
    assert e2 and "no such file" in missing and e3 and "outside" in outside


def test_document_libraries_are_on_the_agents_path(tmp_path):
    ctx, _ = _ctx(tmp_path)
    out, is_error = asyncio.run(run_tool(ctx, "Bash", {"command": "python3 -c 'import docx, openpyxl, pptx, pypdf, reportlab; print(\"ok\")'"}))
    assert not is_error and out.strip() == "ok", out


def test_document_skills_are_built_in():
    names = {s.name for s in skills_mod.all_skills(1)}
    assert {"docx", "xlsx", "pptx", "pdf", "html-prototype"} <= names


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap is not installed")
def test_a_users_skill_scripts_are_readable_in_the_sandbox(tmp_path, monkeypatch):
    from app.agents import tools as tools_mod

    data = tmp_path / "data"
    monkeypatch.setattr(tools_mod, "DATA_DIR", data)
    monkeypatch.setattr(skills_mod, "DATA_DIR", data)
    skill = skills_mod.install_text(1, "---\nname: s1\ndescription: d\n---\nRun scripts/hello.sh\n")
    (skill.path / "scripts").mkdir()
    (skill.path / "scripts/hello.sh").write_text("echo from-skill\n")
    ws = data / "workspaces" / "chat-1"
    ws.mkdir(parents=True)
    ctx = ToolContext(workspace=ws, sandbox="bwrap", read_roots=[skill.path])
    out, is_error = asyncio.run(run_tool(ctx, "Bash", {"command": f"bash {skill.path}/scripts/hello.sh; touch {skill.path}/x 2>&1 | tail -1"}))
    assert "from-skill" in out and not (skill.path / "x").exists()  # readable, not writable


def _github_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(f"skills-main/{name}", text)
    return buf.getvalue()


def test_a_skill_from_a_github_link(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    archive = _github_zip({"skills/demo/SKILL.md": "---\nname: demo\ndescription: A demo skill.\n---\nRun scripts/x.py",
                           "skills/demo/scripts/x.py": "print(1)", "skills/other/SKILL.md": "---\nname: other\ndescription: o\n---\n",
                           "README.md": "repo"})
    asked = []

    def handler(request):
        asked.append(str(request.url))
        if request.url.host == "codeload.github.com" and request.url.path == "/acme/skills/zip/refs/heads/main":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    monkeypatch.setattr(skills_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    skill = asyncio.run(skills_mod.import_from_url(1, "https://github.com/acme/skills/tree/main/skills/demo"))
    assert skill.name == "demo" and (skill.path / "scripts/x.py").read_text() == "print(1)"
    assert not (skill.path.parent / "other").exists()  # only the folder that was asked for
    same = asyncio.run(skills_mod.import_from_url(1, "https://github.com/acme/skills/blob/main/skills/demo/SKILL.md"))
    assert same.name == "demo"
    for bad in ("https://example.com/x", "https://github.com/acme/skills/tree/main/nothing-here"):
        with pytest.raises(skills_mod.SkillError):
            asyncio.run(skills_mod.import_from_url(1, bad))


# ---------------------------------------------------------------- attachments and preview in a chat

class Fake:
    prompts: list[str] = []

    def __init__(self, provider, secret, base_url):
        pass

    async def close(self):
        pass

    async def probe(self, model):
        return None

    async def models_meta(self):
        return [{"id": "m-1", "price_out": 5.0, "context": 100000, "tools": True, "kind": ""}]

    async def create(self, req):
        Fake.prompts.append(req.messages[-1]["content"] if isinstance(req.messages[-1]["content"], str) else "")
        if any(m["role"] == "assistant" for m in req.messages):
            return LLMResponse([{"type": "text", "text": "Here it is."}], "end", 5, 5)
        return LLMResponse([{"type": "tool_use", "id": "s1", "name": "SendFile",
                             "input": {"path": "uploads/sales.xlsx", "caption": "your table"}}], "tool_use", 5, 5)


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "off")
    monkeypatch.setattr(department, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm, "CLIENT_FACTORY", Fake)
    Fake.prompts = []
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}).json()[
            "access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        for key in client.get("/api/keys").json()["keys"]:
            client.delete(f"/api/keys/{key['id']}")
        for role in client.get("/api/agent-roles").json():
            client.delete(f"/api/agent-roles/{role['role']}")
        client.post("/api/keys", json={"provider": "groq", "key": "sk-groq-1111"})
        client.put("/api/agent-roles/Orchestrator", json={"provider": "groq", "model": "m-1"})
        yield client


def _xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Продажи"
    ws.append(["Месяц", "Выручка"])
    ws.append(["Январь", 120.0])
    ws.append(["Февраль", 140.5])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_attachments_go_to_the_workspace_and_the_agent_sends_a_file_back(api):
    r = api.post("/api/chats", data={"prompt": "Посчитай итог", "mode": "single"},
                 files=[("files", ("sales.xlsx", _xlsx(), "application/octet-stream")),
                        ("files", ("../../evil name?.csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.status_code == 201, r.text
    chat_id = r.json()["id"]
    deadline = time.monotonic() + 20
    while api.get(f"/api/chats/{chat_id}").json()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.15)
    assert "uploads/sales.xlsx" in Fake.prompts[0] and "uploads/evil name_.csv" in Fake.prompts[0]
    files = [f["path"] for f in api.get(f"/api/chats/{chat_id}/files").json()]
    assert files == ["uploads/evil name_.csv", "uploads/sales.xlsx"]  # names cleaned, nothing outside
    events = api.get(f"/api/chats/{chat_id}/events").json()
    att = next(e for e in events if e["type"] == "attachment")
    assert att["data"]["path"] == "uploads/sales.xlsx" and att["data"]["kind"] == "sheet"
    table = api.get(f"/api/chats/{chat_id}/preview", params={"path": "uploads/sales.xlsx"}).json()
    assert table["sheets"][0]["name"] == "Продажи"
    assert table["sheets"][0]["rows"] == [["Месяц", "Выручка"], ["Январь", "120"], ["Февраль", "140.5"]]
    csv = api.get(f"/api/chats/{chat_id}/preview", params={"path": "uploads/evil name_.csv"}).json()
    assert csv["sheets"][0]["rows"] == [["a", "b"], ["1", "2"]]
    # a follow-up with a file as a form, too
    r = api.post(f"/api/chats/{chat_id}/messages", data={"prompt": "И этот"},
                 files=[("files", ("sales.xlsx", _xlsx(), "application/octet-stream"))])
    assert r.status_code == 201, r.text
    deadline = time.monotonic() + 20
    while api.get(f"/api/chats/{chat_id}").json()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.15)
    assert "uploads/sales (2).xlsx" in Fake.prompts[-2] or any("sales (2).xlsx" in p for p in Fake.prompts)
