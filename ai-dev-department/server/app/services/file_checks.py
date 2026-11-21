"""Catch files an agent did not finish (chat models sometimes stop early or
write "...rest of the code"). Text-only checks, no code is executed:

- placeholder comments ("...", "rest of the code", "unchanged", ...)
- Python must compile, JSON must parse
- brackets must balance in C-like languages and CSS
- an HTML document that opens <html>/<body> must close it
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

_COMMENT = r"(?:#|//|/\*|<!--|--|;|\*)"
_PLACEHOLDER = re.compile(
    _COMMENT + r"\s*(?:\.{3}|…)"  # "// ...", "# …"
    r"|" + _COMMENT + r".*\b(?:rest of (?:the )?(?:code|file|implementation|class|function|component)"
    r"|existing code|remains? (?:the )?(?:same|unchanged)|same as (?:before|above)|unchanged"
    r"|omitted for brevity|truncated|and so on|etc\.? as before"
    r"|остальн\w* (?:код|без изменений|как было)|без изменений|аналогично выше)\b",
    re.IGNORECASE,
)
_LONE_ELLIPSIS = re.compile(r"^\s*(?:\.{3}|…)\s*$")
_BRACE_LANGS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".c", ".h", ".cpp", ".hpp", ".cc",
                ".cs", ".go", ".rs", ".kt", ".kts", ".swift", ".php", ".scala", ".dart"}
_CSS_LANGS = {".css", ".scss", ".less"}
_STRINGS = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`', re.DOTALL)
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def check_complete(path: str, content: str) -> str | None:
    """Return a reason if `content` looks unfinished, else None."""
    ext = PurePosixPath(path).suffix.lower()
    prose = ext in (".md", ".txt", ".rst", ".adoc", ".csv", "")
    for n, line in enumerate(content.splitlines() if not prose else [], start=1):
        if _PLACEHOLDER.search(line) or (ext != ".py" and _LONE_ELLIPSIS.match(line)):
            return f"line {n} is a placeholder ({line.strip()[:60]!r}) - write the full content"
    if ext == ".py":
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            return f"Python syntax error at line {exc.lineno}: {exc.msg} (file cut off or broken)"
    elif ext == ".json":
        try:
            json.loads(content)
        except ValueError as exc:
            return f"invalid JSON: {exc}"
    elif ext in _BRACE_LANGS or ext in _CSS_LANGS:
        unescaped = re.sub(r"\\.", "", content)  # \( \{ \" etc. are not brackets/quotes
        code = _COMMENTS.sub("", _STRINGS.sub('""', unescaped))
        pairs = {"{": "}"} if ext in _CSS_LANGS else {"{": "}", "(": ")", "[": "]"}
        closing = {v: k for k, v in pairs.items()}
        stack: list[str] = []
        for ch in code:
            if ch in pairs:
                stack.append(ch)
            elif ch in closing:
                if not stack or stack[-1] != closing[ch]:
                    return f"unbalanced '{ch}' (file cut off or broken)"
                stack.pop()
        if stack:
            return f"{len(stack)} unclosed '{stack[-1]}' at the end (file looks cut off)"
    elif ext in (".html", ".htm"):
        low = content.lower()
        for tag in ("html", "body"):
            if f"<{tag}" in low and f"</{tag}>" not in low:
                return f"<{tag}> is never closed (file looks cut off)"
    return None
