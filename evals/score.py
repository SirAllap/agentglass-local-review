"""Score an agent's findings against what a case planted. Pure: no model, no
files. A finding matches an expected bug when it is in the same file, within a
few lines of it (or has no line), and says one of the words that name the bug,
in its title or its body."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review  # noqa: E402

SLACK = 3
RANK = {s: i for i, s in enumerate(reversed(review.SEVERITIES))}  # low < medium < high < critical


@dataclass
class Score:
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    underrated: list[str] = field(default_factory=list)
    noise: list[review.Finding] = field(default_factory=list)

    @property
    def serious_noise(self) -> int:
        return sum(1 for n in self.noise if n.severity in ("critical", "high"))

    @property
    def passed(self) -> bool:
        # A case with nothing planted is a trap for false alarms: it passes
        # only if nothing serious was raised. One with a planted bug passes when
        # it was found and rated high enough; extra noise is reported, not failed.
        if not self.found and not self.missed:
            return self.serious_noise == 0
        return not self.missed and not self.underrated


def matches(finding: review.Finding, exp: dict) -> bool:
    if finding.path != exp["path"]:
        return False
    lo, hi = exp["lines"]
    if finding.line is not None and not (lo - SLACK <= finding.line <= hi + SLACK):
        return False
    text = f"{finding.title} {finding.body}".lower()
    return any(w in text for w in exp["any"])


def score(findings: list[review.Finding], expect: list[dict]) -> Score:
    s = Score()
    used: set[int] = set()
    for exp in expect:
        hit = next((i for i, f in enumerate(findings) if i not in used and matches(f, exp)), None)
        if hit is None:
            s.missed.append(exp["id"])
            continue
        used.add(hit)
        s.found.append(exp["id"])
        if RANK[findings[hit].severity] < RANK[exp["severity_min"]]:
            s.underrated.append(exp["id"])
    s.noise = [f for i, f in enumerate(findings) if i not in used]
    return s
