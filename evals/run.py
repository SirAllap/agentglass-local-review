#!/usr/bin/env python3
"""Run the eval cases through the real review path with a real agent, and say
how many planted bugs it found and how much noise it made.

    python3 evals/run.py --list
    python3 evals/run.py --agent claude --cap 0.5            # every case
    python3 evals/run.py --agent claude --only sql-injection

This SPENDS MONEY (each case is one review, held to --cap). It reads nothing
from GitHub: the pull request, the diff and the code come from evals/cases,
and the agent runs in the same cage as a real review. Needs bwrap and the
agent signed in. Exit status is 0 only if every case passed."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cage  # noqa: E402
import fixtures  # noqa: E402
import github  # noqa: E402
import review  # noqa: E402
import score  # noqa: E402


def run_case(case: fixtures.Case, agent: str, model: str, cap: float, work: Path) -> tuple[review.Outcome, score.Score | None]:
    pr = github.Pr(repo="acme/orbit", number=1, title=case.title, author="dev", head="a" * 40, base_ref="main",
                   url="https://github.com/acme/orbit/pull/1", draft=False)
    ctx = {"title": case.title, "author": "dev", "head": "feature/change", "base": "main", "body": case.body,
           "commits": [case.title], "threads": []}

    def snapshot(repo, sha, dest, number=None):
        shutil.copytree(case.head, dest, dirs_exist_ok=True)
        return 0

    with mock.patch.object(review.github, "context", return_value=ctx), \
         mock.patch.object(review.github, "diff", return_value=fixtures.diff_of(case)), \
         mock.patch.object(review.github, "snapshot", side_effect=snapshot):
        out = review.run(pr, cage.ADAPTERS[agent], model, cap, None, [], work)
    return out, (score.score(out.findings, case.expect) if out.ok else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent", default="claude", choices=sorted(cage.ADAPTERS))
    ap.add_argument("--model", default="")
    ap.add_argument("--cap", type=float, default=0.5, help="USD per case")
    ap.add_argument("--only", action="append", default=[], help="a case id (repeatable)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    cases = [c for c in fixtures.load_all() if not a.only or c.id in a.only]
    if a.list:
        for c in cases:
            print(f"{c.id:16} {len(c.expect)} planted  {c.title}")
        return 0
    ok = cage.agent_starts(cage.ADAPTERS[a.agent])
    if not ok[0]:
        print(f"cannot run: {ok[1]}", file=sys.stderr)
        return 2
    total, passed = 0.0, 0
    print(f"{'case':16} {'result':7} {'found':7} {'noise':>5} {'serious':>7} {'cost':>7}")
    with tempfile.TemporaryDirectory() as d:
        for c in cases:
            out, s = run_case(c, a.agent, a.model, a.cap, Path(d))
            total += out.cost_usd or 0
            if s is None:
                print(f"{c.id:16} {'ERROR':7} {out.error}")
                continue
            passed += s.passed
            print(f"{c.id:16} {'pass' if s.passed else 'FAIL':7} {len(s.found)}/{len(c.expect):<5} {len(s.noise):>5} {s.serious_noise:>7} ${out.cost_usd or 0:>6.2f}")
            for m in s.missed:
                print(f"    missed: {m}")
            for m in s.underrated:
                print(f"    rated too low: {m}")
    print(f"\n{passed}/{len(cases)} passed, ${total:.2f} spent")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
