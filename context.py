"""What the card and CI add to a review, gathered here and sent to the model
only for a repository that turned it on. Everything is read through the app's
own routes: its ClickUp and Jira connections, its pull request checks and the
failures it has already read. Nothing here writes, and nothing needs a login
of the plugin's own.

A failure is described by what was SEEN about it, never by why: "also failing
on main" is a fact the app read, "this change caused it" is a guess. The text
is written by other people (a card, a CI log), so the prompt calls it evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import github

CARD_CAP = 6000
CI_CAP = 4000

CARD_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")

# What a secret looks like, by kind. Only the kind is ever reported or kept.
SECRET_SHAPES = [
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an API key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("a password or secret in an assignment",
     re.compile(r"(?i)\b(?:pass(?:word|wd)?|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_.=-]{12,}")),
]


@dataclass
class Gathered:
    text: str = ""
    blocked: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)  # "card", "CI"


def secrets(text: str) -> list[str]:
    return [kind for kind, rx in SECRET_SHAPES if rx.search(text)]


def card_refs(*texts: str) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for m in CARD_RE.findall(t or ""):
            if m not in seen:
                seen.append(m)
    return seen


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap].rstrip() + "\n[cut]"


def card(refs: list[str]) -> str | None:
    """The first card any connected tracker knows, as markdown."""
    for ref in refs[:3]:
        for path in ("/clickup/task", "/jira/task"):
            d = github.app_get(path, {"id": ref}, timeout=30)
            if not d:
                continue
            task = d.get("task") or {}
            title = task.get("title") or task.get("name")
            if not title:
                continue
            out = [f"## Card {ref}: {title}"]
            if task.get("status"):
                out.append(f"Status: {task['status'] if isinstance(task['status'], str) else task['status'].get('name', '')}")
            desc = d.get("description") or task.get("description")
            if desc:
                out.append(f"\n{desc}")
            for cl in d.get("checklists") or []:
                out.append(f"\n{cl.get('name', 'Checklist')}:")
                out += [f"- [{'x' if i.get('done') else ' '}] {i.get('name', '')}" for i in cl.get("items") or []]
            comments = d.get("comments") or []
            if comments:
                out.append("\nComments:")
                out += [f"- {c.get('who', '?')}: {' '.join(str(c.get('text', '')).split())[:400]}" for c in comments[-8:]]
            return _clip("\n".join(out), CARD_CAP)
    return None


def _sentence(v: dict) -> str:
    k = v.get("kind")
    if k == "main":
        return "also failing on the default branch: not caused by this change"
    if k == "flaky":
        return "failed and then passed on the same commit: flaky"
    if k in ("others", "prs"):
        return f"also failing on {v.get('prs', len(v.get('nums') or [])) or 'other'} other pull request(s): probably not this change"
    if k == "this-pr":
        return "not seen anywhere else: this change may have caused it"
    return "seen once, no history to compare: unknown"


def _job_id(url: str) -> str | None:
    m = re.search(r"/job/(\d+)", url or "")
    return m.group(1) if m else None


def ci(repo: str, number: int) -> str | None:
    """The failing checks of the pull request and what the app has seen about
    each failure. None when the app has no checkout or cannot say."""
    root = github.app_root(repo)
    if not root:
        return None
    roll = github.app_get("/prs/rollup", {"root": root, "number": str(number)}, timeout=30)
    if not roll:
        return None
    failing = [c for c in roll.get("all") or [] if c.get("state") == "failure" and not c.get("cancelled")]
    if not failing:
        return "## CI\n\nThere are no failing checks."
    jobs = {c["name"]: _job_id(c.get("url", "")) for c in failing}
    wanted = [j for j in jobs.values() if j]
    cached = (github.app_get("/prs/check-failures-cached", {"root": root, "jobs": ",".join(wanted)}, timeout=30) or {}).get("summaries") or {}
    lens = None
    out = ["## CI: failing checks", "", "What the app has read about each failure. A verdict says what was seen, not why."]
    for c in failing:
        s = cached.get(jobs.get(c["name"]) or "")
        if s and s.get("titles"):
            out.append(f"\n- {c['name']}:")
            for t, v in zip(s["titles"], list(s.get("verdicts") or []) + [{}] * len(s["titles"])):
                out.append(f"  - {t} — {_sentence(v or {})}")
            continue
        if lens is None:
            lens = (github.app_get("/prs/failing-tests", {"root": root}, timeout=30) or {}).get("rows") or []
        rows = [r for r in lens if r.get("check") == c["name"]]
        if rows:
            out.append(f"\n- {c['name']}:")
            out += [f"  - {r.get('title')} — {_sentence(r.get('verdict') or {})}" for r in rows[:5]]
        else:
            out.append(f"\n- {c['name']}: failing, and the app has not read why yet")
    return _clip("\n".join(out), CI_CAP)


def gather(pr: github.Pr, branch: str, body: str) -> Gathered:
    """The card and the CI picture for `pr`. A secret in either blocks: the
    kinds found are returned and nothing of the text is kept."""
    parts, used = [], []
    c = card(card_refs(pr.title, branch, body))
    if c:
        parts.append(c)
        used.append("card")
    t = ci(pr.repo, pr.number)
    if t:
        parts.append(t)
        used.append("CI")
    text = "\n\n".join(parts)
    found = secrets(text)
    if found:
        return Gathered(blocked=found)
    return Gathered(text=text, used=used)
