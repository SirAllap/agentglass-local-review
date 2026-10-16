"""Load the eval cases and turn a case into what a review is handed: the
unified diff, the pull request description, and the head tree. A case is a
folder with `case.json`, `base/` and `head/`; the diff is made here from the
two trees, so it can never disagree with the files."""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

CASES = Path(__file__).resolve().parent / "cases"


@dataclass
class Case:
    id: str
    root: Path
    title: str
    body: str
    expect: list[dict]

    @property
    def head(self) -> Path:
        return self.root / "head"

    @property
    def base(self) -> Path:
        return self.root / "base"


def load_all() -> list[Case]:
    out = []
    for d in sorted(p for p in CASES.iterdir() if (p / "case.json").is_file()):
        j = json.loads((d / "case.json").read_text())
        out.append(Case(id=d.name, root=d, title=j["title"], body=j.get("body", ""), expect=j.get("expect", [])))
    return out


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def diff_of(case: Case) -> str:
    before, after = _files(case.base), _files(case.head)
    out = []
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path, ""), after.get(path, "")
        if a == b:
            continue
        body = list(difflib.unified_diff(a.splitlines(), b.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=3))
        out.append(f"diff --git a/{path} b/{path}\n" + "\n".join(body))
    return "\n".join(out) + "\n"


def added_lines(case: Case) -> dict[str, set[int]]:
    """Line numbers, in the head files, that the diff adds or changes."""
    out: dict[str, set[int]] = {}
    path, new = None, 0
    for line in diff_of(case).splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            out[path] = set()
        elif m := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line):
            new = int(m.group(1))
        elif path and line.startswith("+") and not line.startswith("+++"):
            out[path].add(new)
            new += 1
        elif path and line.startswith(" "):
            new += 1
    return out
