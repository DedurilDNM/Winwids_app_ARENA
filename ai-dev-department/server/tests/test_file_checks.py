"""Unfinished-file detection: catches cut-off files, accepts valid code."""

from __future__ import annotations

import pytest

from app.services.agent_output import AgentOutputError, parse_agent_output
from app.services.file_checks import check_complete

VALID = {
    "src/app.py": "from typing import Protocol\n\n\nclass P(Protocol):\n    def run(self) -> None: ...\n\n\nx = {'a': [1, (2, 3)]}\n",
    "src/util.js": "const re = /\\d{2}[a-z]+\\(/g;\nconst t = `value ${x} {`;\nfunction f(a) { return [a, (a)]; } // } stray in comment\n/* { */\n",
    "src/App.tsx": "export function App(): JSX.Element {\n  return <div className=\"a\">{items.map((i) => <b key={i}>{i}</b>)}</div>;\n}\n",
    "styles/main.css": "a { color: red; }\n@media (max-width: 1px) { b { content: '{'; } }\n",
    "data.json": '{"a": [1, 2, {"b": "}"}]}',
    "index.html": "<!doctype html><html><body><p>Hi</p></body></html>\n",
    "README.md": "# Title\n\n* Config remains unchanged\n...\n// rest of the code is in src/\n",
    "notes.txt": "...\n",
    "Main.java": "class Main { public static void main(String[] a) { System.out.println(\"}\"); } }\n",
}

BROKEN = {
    "src/app.py": ("def f():\n    return [1, 2,\n", "syntax"),
    "src/util.js": ("function f() {\n  if (x) {\n    run();\n", "unclosed"),
    "src/a.ts": ("export const x = 1;\n// ... rest of the code\n", "placeholder"),
    "src/b.js": ("const a = 1;\n...\n", "placeholder"),
    "src/c.py": ("x = 1\n# остальной код без изменений\n", "placeholder"),
    "data.json": ('{"a": [1, 2', "JSON"),
    "index.html": ("<html><body><p>Hi</p>\n", "never closed"),
    "styles/main.css": ("a { color: red;\n", "unclosed"),
    "page.html": ("<div>\n<!-- ... -->\n</div>\n", "placeholder"),
}


@pytest.mark.parametrize("path", list(VALID))
def test_valid_files_pass(path):
    assert check_complete(path, VALID[path]) is None, path


@pytest.mark.parametrize("path", list(BROKEN))
def test_unfinished_files_are_caught(path):
    content, word = BROKEN[path]
    reason = check_complete(path, content)
    assert reason and word.lower() in reason.lower(), (path, reason)


def test_parser_rejects_unfinished_file_with_a_clear_message():
    with pytest.raises(AgentOutputError, match="unfinished: src/a.py"):
        parse_agent_output("FILE: src/a.py\n```python\ndef f(:\n```\nSTATUS: DONE\nSUMMARY: x")
    # BLOCKED replies are not checked (nothing is committed)
    assert parse_agent_output("FILE: a.py\n```\ndef f(:\n```\nSTATUS: BLOCKED\nSUMMARY: x").status == "BLOCKED"
