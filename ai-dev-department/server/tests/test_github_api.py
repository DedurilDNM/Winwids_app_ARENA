"""GitHub connection API: token validation, encrypted storage, repo check."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import github as github_api
from app.connectors.github import GitHubConnector
from app.main import app
from tests.fake_github import FakeGitHub
from tests.test_projects import _auth, _fresh_user


@pytest.fixture()
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github_api, "connector_for",
                        lambda token: GitHubConnector(token=token, transport=fake.transport()))
    return fake


def test_connect_check_disconnect(gh) -> None:
    with TestClient(app) as client:
        h = _auth(_fresh_user(client, "gh1"))
        assert client.get("/api/github", headers=h).json() == {"connected": False, "login": None, "token_hint": None}

        bad = client.put("/api/github", json={"token": "ghp_wrong_token_000000000"}, headers=h)
        assert bad.status_code == 400 and "rejected" in bad.json()["detail"]

        ok = client.put("/api/github", json={"token": gh.token}, headers=h)
        assert ok.status_code == 200 and ok.json() == {"connected": True, "login": "octo", "token_hint": "…7890"}
        status = client.get("/api/github", headers=h).json()
        assert status["connected"] and gh.token not in str(status)

        check = client.get("/api/github/repo-check", params={"repo": "https://github.com/octo/website.git"}, headers=h).json()
        assert check["ok"] and check["can_push"] and check["default_branch"] == "main"
        missing = client.get("/api/github/repo-check", params={"repo": "octo/other"}, headers=h).json()
        assert not missing["ok"] and "not found" in missing["message"]
        assert client.get("/api/github/repo-check", params={"repo": "bad repo"}, headers=h).status_code == 400

        assert client.delete("/api/github", headers=h).status_code == 204
        assert client.get("/api/github", headers=h).json()["connected"] is False


def test_repo_check_read_only_and_empty(monkeypatch) -> None:
    for fake, expected in ((FakeGitHub(can_push=False), "Read-only"), (FakeGitHub(empty=True), "empty")):
        monkeypatch.setattr(github_api, "connector_for",
                            lambda token, f=fake: GitHubConnector(token=token, transport=f.transport()))
        with TestClient(app) as client:
            h = _auth(_fresh_user(client, f"gh-{expected}"))
            client.put("/api/github", json={"token": fake.token}, headers=h)
            check = client.get("/api/github/repo-check", params={"repo": "octo/website"}, headers=h).json()
            assert not check["ok"] and expected in check["message"]


def test_token_is_encrypted_at_rest(gh) -> None:
    import asyncio

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ConnectorCredential

    with TestClient(app) as client:
        h = _auth(_fresh_user(client, "gh-enc"))
        client.put("/api/github", json={"token": gh.token}, headers=h)

    async def rows():
        async with SessionLocal() as s:
            return (await s.scalars(select(ConnectorCredential))).all()

    assert all(gh.token.encode() not in r.encrypted_payload for r in asyncio.run(rows()))
