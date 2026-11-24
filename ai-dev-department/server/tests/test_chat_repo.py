"""A chat on a GitHub repository: clone, a commit per turn, push + pull request.

Real git against a local bare repository as «GitHub» (GITHUB_GIT_URL points to it);
the GitHub API (repository check, pull requests) is the in-memory FakeGitHub."""

from __future__ import annotations

import re
import subprocess
import time

import pytest

from app.agents import department, llm, skills as skills_mod
from app.agents.llm import LLMResponse
from app.connectors.github import GitHubConnector
from tests.fake_github import FakeGitHub

TOKEN = "ghp_test_token_1234567890"


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


class Fake:
    def __init__(self, provider, secret, base_url):
        pass

    async def close(self):
        pass

    async def probe(self, model):
        return None

    async def models_meta(self):
        return []

    async def create(self, req):
        last = max(i for i, m in enumerate(req.messages) if m["role"] == "user" and isinstance(m["content"], str))
        prompt = req.messages[last]["content"]
        turn = sum(1 for m in req.messages[last:] if m["role"] == "assistant")
        if turn == 0 and "hello" in prompt:
            return LLMResponse([{"type": "tool_use", "id": "e1", "name": "Edit", "input": {
                "path": "README.md", "old_string": "# Website", "new_string": "# Website\n\nHello"}}], "tool_use", 5, 5)
        if turn == 0 and "page" in prompt:
            return LLMResponse([{"type": "tool_use", "id": "w1", "name": "Write", "input": {
                "path": "index.html", "content": "<h1>Page</h1>\n"}}], "tool_use", 5, 5)
        return LLMResponse([{"type": "text", "text": f"Done: {prompt.splitlines()[0]}"}], "end", 5, 5)


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import github as github_api
    from app.config import get_settings
    from app.main import app

    remotes = tmp_path / "remotes"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("# Website\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-qm", "init")
    (remotes / "octo").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remotes / "octo" / "website.git")], check=True)

    fake = FakeGitHub(repo="octo/website")
    monkeypatch.setattr(github_api, "connector_for", lambda token: GitHubConnector(token=token, transport=fake.transport()))
    monkeypatch.setattr(get_settings(), "GITHUB_GIT_URL", f"file://{remotes}")
    monkeypatch.setattr(get_settings(), "AGENT_BASH_SANDBOX", "off")
    monkeypatch.setattr(department, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(skills_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(llm, "CLIENT_FACTORY", Fake)
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"email": "admin@local", "password": "admin-test-pass"}).json()[
            "access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        for key in client.get("/api/keys").json()["keys"]:
            client.delete(f"/api/keys/{key['id']}")
        for role in client.get("/api/agent-roles").json():
            client.delete(f"/api/agent-roles/{role['role']}")
        client.delete("/api/github")
        client.post("/api/keys", json={"provider": "groq", "key": "sk-groq-1111"})
        client.put("/api/agent-roles/Orchestrator", json={"provider": "groq", "model": "m-1"})
        client.put("/api/agent-roles/Coder", json={"provider": "groq", "model": "m-1"})
        yield client, fake, remotes / "octo" / "website.git"


def _idle(api, chat_id, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chat = api.get(f"/api/chats/{chat_id}").json()
        if chat["status"] != "running":
            return chat
        time.sleep(0.15)
    raise AssertionError(chat)


def test_a_chat_on_a_repository_commits_each_turn_and_opens_a_pull_request(api):
    client, fake, remote = api
    # GitHub not connected yet
    r = client.post("/api/chats", json={"prompt": "say hello", "mode": "single", "repo": "octo/website"})
    assert r.status_code == 409
    assert client.put("/api/github", json={"token": TOKEN}).status_code == 200
    assert client.get("/api/github/repos").json()[0]["full_name"] == "octo/website"

    r = client.post("/api/chats", json={"prompt": "say hello in the readme", "mode": "single", "repo": "octo/website"})
    assert r.status_code == 201, r.text
    chat_id = r.json()["id"]
    chat = _idle(client, chat_id)
    assert chat["turns"][0]["status"] == "done", chat["turns"][0]
    assert chat["repo"] == "octo/website" and chat["branch"] == f"aidd/chat-{chat_id}" and chat["base_branch"] == "main"
    assert chat["ahead"] == 1
    ws = department.chat_workspace(chat_id)
    assert (ws / "README.md").read_text() == "# Website\n\nHello\n"
    assert TOKEN not in (ws / ".git" / "config").read_text()  # the token is never stored
    committed = _git(ws, "show", "--name-only", "--format=%s", "HEAD").split()
    assert committed[:2] == ["AIDD:", "say"] and "README.md" in committed
    assert not any(x.startswith((".aidd-git", "uploads", ".home")) for x in _git(ws, "ls-files").split())
    events = client.get(f"/api/chats/{chat_id}/events").json()
    assert [e["data"]["status"] for e in events if e["type"] == "repo"] == ["cloning", "ready", "committed"]

    pr = client.post(f"/api/chats/{chat_id}/pr")
    assert pr.status_code == 200, pr.text
    assert pr.json()["pr_url"] == "https://github.com/octo/website/pull/1"
    assert fake.pulls[0]["head"] == f"aidd/chat-{chat_id}" and fake.pulls[0]["base"] == "main"
    assert "say hello in the readme" in fake.pulls[0]["body"]
    remote_log = subprocess.run(["git", "log", "--format=%s", f"aidd/chat-{chat_id}"], cwd=remote, capture_output=True,
                                text=True, check=True).stdout.splitlines()
    assert remote_log[0].startswith("AIDD: say hello") and remote_log[-1] == "init"

    # a follow-up: a second commit, the same pull request is updated
    client.post(f"/api/chats/{chat_id}/messages", json={"prompt": "add a page", "mode": "single"})
    chat = _idle(client, chat_id)
    assert chat["ahead"] == 2 and chat["pr_url"].endswith("/pull/1")
    again = client.post(f"/api/chats/{chat_id}/pr")
    assert again.status_code == 200 and again.json()["pr_url"].endswith("/pull/1") and len(fake.pulls) == 1
    files = subprocess.run(["git", "ls-tree", "--name-only", f"aidd/chat-{chat_id}"], cwd=remote, capture_output=True,
                           text=True, check=True).stdout.split()
    assert files == ["README.md", "index.html"]


def test_repository_checks(api, monkeypatch):
    client, fake, remote = api
    client.put("/api/github", json={"token": TOKEN})
    assert client.post("/api/chats", json={"prompt": "x", "repo": "not a repo"}).status_code == 400
    fake.can_push = False
    r = client.post("/api/chats", json={"prompt": "x", "repo": "octo/website"})
    assert r.status_code == 400 and "cannot push" in r.json()["detail"]
    fake.can_push = True
    # a chat without changes has nothing to send; a chat without a repository has no PR at all
    chat_id = client.post("/api/chats", json={"prompt": "just answer", "mode": "single", "repo": "octo/website"}).json()["id"]
    _idle(client, chat_id)
    assert client.post(f"/api/chats/{chat_id}/pr").status_code == 400
    plain = client.post("/api/chats", json={"prompt": "just answer", "mode": "single"}).json()["id"]
    _idle(client, plain)
    assert client.post(f"/api/chats/{plain}/pr").status_code == 400
    assert re.match(r"^aidd/chat-\d+$", client.get(f"/api/chats/{chat_id}").json()["branch"])


def test_screenshots_are_never_committed(tmp_path):
    import subprocess

    from app.agents import repo as repo_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=ws, check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@x")):
        subprocess.run(["git", "config", k, v], cwd=ws, check=True)
    (ws / ".git/info").mkdir(parents=True, exist_ok=True)
    (ws / ".git/info/exclude").write_text(".aidd-git/\n")  # an older chat: no screenshots/ entry yet
    (ws / "screenshots").mkdir()
    (ws / "screenshots/page-desktop.png").write_bytes(b"png")
    (ws / "page.html").write_text("<p>x</p>")
    done = repo_mod.commit_turn(ws, "turn")
    assert done and done["files"] == 1
    tracked = subprocess.run(["git", "ls-files"], cwd=ws, capture_output=True, text=True).stdout.split()
    assert tracked == ["page.html"]
