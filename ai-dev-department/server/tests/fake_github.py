"""In-memory GitHub REST API (just the endpoints the server uses), as an
httpx.MockTransport. Models commits/trees/refs so multi-file commits,
branches and pull requests behave like the real thing."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx


def _sha(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


class FakeGitHub:
    def __init__(self, *, token: str = "ghp_test_token_1234567890", login: str = "octo",
                 repo: str = "octo/website", can_push: bool = True, empty: bool = False) -> None:
        self.token, self.login, self.repo, self.can_push = token, login, repo, can_push
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.calls: list[str] = []
        if not empty:
            self.refs["main"] = self._commit({"README.md": "# Website\n"}, [], "init")

    # --- model -------------------------------------------------------------
    def _commit(self, files: dict[str, str], parents: list[str], message: str) -> str:
        tree_sha = _sha("tree", json.dumps(files, sort_keys=True))
        self.trees[tree_sha] = dict(files)
        sha = _sha("commit", tree_sha, *parents, message)
        self.commits[sha] = {"tree": tree_sha, "parents": parents, "message": message}
        return sha

    def files(self, branch: str) -> dict[str, str]:
        return self.trees[self.commits[self.refs[branch]]["tree"]]

    def log(self, branch: str) -> list[str]:
        out, sha = [], self.refs.get(branch)
        while sha:
            out.append(self.commits[sha]["message"])
            parents = self.commits[sha]["parents"]
            sha = parents[0] if parents else None
        return out

    # --- transport ---------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        path, m = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/user":
            return httpx.Response(200, json={"login": self.login})
        prefix = f"/repos/{self.repo}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(prefix):]
        if rest == "" and m == "GET":
            return httpx.Response(200, json={"full_name": self.repo, "default_branch": "main",
                                             "private": True, "permissions": {"push": self.can_push}})
        if (mm := re.fullmatch(r"/git/ref/heads/(.+)", rest)) and m == "GET":
            if not self.refs:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            sha = self.refs.get(mm.group(1))
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404, json={"message": "Not Found"})
        if rest == "/git/refs" and m == "POST":
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if (mm := re.fullmatch(r"/git/refs/heads/(.+)", rest)) and m == "PATCH":
            if not self.can_push:
                return httpx.Response(403, json={"message": "Resource not accessible"})
            self.refs[mm.group(1)] = body["sha"]
            return httpx.Response(200, json={})
        if (mm := re.fullmatch(r"/git/trees/(\w+)", rest)) and m == "GET":
            tree = self.trees[self.commits[mm.group(1)]["tree"]]
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob"} for p in sorted(tree)]})
        if (mm := re.fullmatch(r"/git/commits/(\w+)", rest)) and m == "GET":
            return httpx.Response(200, json={"tree": {"sha": self.commits[mm.group(1)]["tree"]}})
        if rest == "/git/trees" and m == "POST":
            files = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                files[e["path"]] = e["content"]
            tree_sha = _sha("tree", json.dumps(files, sort_keys=True))
            self.trees[tree_sha] = files
            return httpx.Response(201, json={"sha": tree_sha})
        if rest == "/git/commits" and m == "POST":
            sha = _sha("commit", body["tree"], *body["parents"], body["message"])
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": sha})
        if (mm := re.fullmatch(r"/contents/(.+)", rest)) and m == "GET":
            files = self.files(request.url.params.get("ref") or "main")
            content = files.get(mm.group(1))
            if content is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})
        if rest == "/pulls" and m == "POST":
            if any(p["head"] == body["head"] for p in self.pulls):
                return httpx.Response(422, json={"message": "A pull request already exists"})
            pr = {"head": body["head"], "base": body["base"], "title": body["title"], "body": body["body"],
                  "html_url": f"https://github.com/{self.repo}/pull/{len(self.pulls) + 1}"}
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        if rest == "/pulls" and m == "GET":
            head = request.url.params.get("head", "").split(":", 1)[-1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"] == head])
        return httpx.Response(404, json={"message": f"unhandled {m} {rest}"})
