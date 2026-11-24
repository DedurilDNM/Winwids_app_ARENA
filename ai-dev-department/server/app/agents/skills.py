"""Skills, as in Claude Code: a folder with SKILL.md (YAML front matter with `name`
and `description`, then the instructions) and optional files next to it.

An agent sees only each skill's name and description; it loads the instructions with
the Skill tool when a task matches, and reads the skill's files with Read when the
instructions say so (progressive disclosure: the context stays small).

Built-in skills ship in app/agents/skills_builtin/; the user's own live in
data/skills/<user id>/ and are added as a SKILL.md text or a .zip.
"""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import DATA_DIR

BUILTIN_DIR = Path(__file__).resolve().parent / "skills_builtin"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_ZIP = 5 * 1024 * 1024


class SkillError(ValueError):
    pass


def user_dir(user_id: int) -> Path:
    return DATA_DIR / "skills" / str(int(user_id))


def parse_skill_md(text: str) -> tuple[dict, str]:
    """(front matter, body). Front matter is required and needs name + description."""
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise SkillError("SKILL.md must start with front matter: ---, name:, description:, ---")
    head, body = m.group(1), m.group(2)
    try:
        import yaml

        meta = yaml.safe_load(head) or {}
    except Exception:
        meta = {}
        for line in head.splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    if not isinstance(meta, dict):
        raise SkillError("the front matter must be key: value lines")
    name = str(meta.get("name") or "").strip()
    desc = " ".join(str(meta.get("description") or "").split())
    if not NAME_RE.match(name):
        raise SkillError("name: lowercase letters, digits and hyphens, up to 64 characters")
    if not desc:
        raise SkillError("description is required: it is how an agent knows when to use the skill")
    meta["name"], meta["description"] = name, desc[:1024]
    return meta, body.strip()


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str  # "builtin" | "user"

    def body(self) -> str:
        return parse_skill_md((self.path / "SKILL.md").read_text(encoding="utf-8"))[1]


def _scan(base: Path, source: str) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if not base.is_dir():
        return out
    for md in sorted(base.glob("*/SKILL.md")):
        try:
            meta, _ = parse_skill_md(md.read_text(encoding="utf-8"))
        except (SkillError, OSError, UnicodeDecodeError):
            continue
        out[meta["name"]] = Skill(meta["name"], meta["description"], md.parent, source)
    return out


def all_skills(user_id: int) -> list[Skill]:
    found = _scan(BUILTIN_DIR, "builtin")
    found.update(_scan(user_dir(user_id), "user"))  # the user's own skill wins over a built-in one
    return sorted(found.values(), key=lambda s: s.name)


class SkillSet:
    """The skills one agent may use."""

    def __init__(self, items: list[Skill]):
        self.items = items

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.items if s.name == name), None)

    @property
    def roots(self) -> list[Path]:
        return [s.path for s in self.items]

    def listing(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.items)


def skill_set(user_id: int, names: list[str] | None) -> SkillSet:
    skills = all_skills(user_id)
    if names is not None:
        wanted = set(names)
        skills = [s for s in skills if s.name in wanted]
    return SkillSet(skills)


def install_text(user_id: int, text: str) -> Skill:
    meta, _ = parse_skill_md(text)
    target = user_dir(user_id) / meta["name"]
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(text, encoding="utf-8")
    return Skill(meta["name"], meta["description"], target, "user")


def install_zip(user_id: int, data: bytes) -> Skill:
    if len(data) > MAX_ZIP:
        raise SkillError("the archive is larger than 5 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("not a zip archive") from None
    names = [n for n in zf.namelist() if not n.endswith("/")]
    md = [n for n in names if n.split("/")[-1] == "SKILL.md"]
    if not md:
        raise SkillError("the archive has no SKILL.md")
    root = min(md, key=lambda n: n.count("/")).rsplit("SKILL.md", 1)[0]  # the folder that holds SKILL.md
    meta, _ = parse_skill_md(zf.read(root + "SKILL.md").decode("utf-8"))
    total = sum(zf.getinfo(n).file_size for n in names)
    if total > 4 * MAX_ZIP:
        raise SkillError("the archive unpacks to more than 20 MB")
    target = user_dir(user_id) / meta["name"]
    tmp = target.with_name(target.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for n in names:
        if not n.startswith(root):
            continue
        rel = n[len(root):]
        dest = (tmp / rel).resolve()
        if tmp.resolve() not in dest.parents:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SkillError(f"unsafe path in the archive: {n}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zf.read(n))
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    return Skill(meta["name"], meta["description"], target, "user")


def remove(user_id: int, name: str) -> bool:
    if not NAME_RE.match(name or ""):
        return False
    target = user_dir(user_id) / name
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


HTTP_TRANSPORT = None  # tests: an httpx transport that serves fake GitHub downloads
GITHUB_TREE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/tree/([^/]+)/(.+?)/?$")
GITHUB_BLOB = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/blob/([^/]+)/(.+/)?SKILL\.md$")
RAW = re.compile(r"^https://raw\.githubusercontent\.com/([\w.-]+)/([\w.-]+)/([^/]+)/(.+/)?SKILL\.md$")


async def import_from_url(user_id: int, url: str) -> Skill:
    """A skill folder from GitHub: https://github.com/<owner>/<repo>/tree/<branch>/<path to the folder>
    (a link to its SKILL.md works too). The folder is downloaded with its files."""
    import httpx

    url = (url or "").strip()
    m = GITHUB_TREE.match(url) or GITHUB_BLOB.match(url) or RAW.match(url)
    if not m:
        raise SkillError("Give a GitHub link to a skill folder: https://github.com/<owner>/<repo>/tree/<branch>/<folder>")
    owner, repo, ref, folder = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip("/")
    archive = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60, transport=HTTP_TRANSPORT) as client:
        resp = await client.get(archive)
        if resp.status_code == 404:  # a tag or a commit instead of a branch
            resp = await client.get(f"https://codeload.github.com/{owner}/{repo}/zip/{ref}")
    if resp.status_code != 200:
        raise SkillError(f"GitHub did not give the repository ({resp.status_code})")
    data = resp.content
    if len(data) > 8 * MAX_ZIP:
        raise SkillError("The repository archive is larger than 40 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("GitHub did not give a zip archive") from None
    names = zf.namelist()
    top = names[0].split("/")[0] + "/" if names else ""
    prefix = f"{top}{folder}/" if folder else top
    wanted = [n for n in names if n.startswith(prefix) and not n.endswith("/")]
    if not any(n == prefix + "SKILL.md" for n in wanted):
        raise SkillError("There is no SKILL.md in that folder")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as small:
        for n in wanted:
            small.writestr(n[len(prefix):], zf.read(n))
    return install_zip(user_id, out.getvalue())

