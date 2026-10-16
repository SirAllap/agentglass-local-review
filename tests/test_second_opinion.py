"""A critical or high finding is checked by one more pass, inside the budget;
findings are merged by fingerprint with how many passes agreed. No model."""
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import review  # noqa: E402
import reviewer  # noqa: E402

F = review.Finding


def out(findings, cost=0.5, ok=True):
    return review.Outcome(ok=ok, findings=findings, cost_usd=cost)


class Merge(unittest.TestCase):
    def test_a_finding_both_passes_raised_agrees_two_of_two(self):
        a = F("high", "Guard after loop", "x", "src/a.ts", 3)
        b = F("high", "Guard  after loop!", "other words", "src/a.ts", 4)
        (m,) = review.merge_passes([("claude", [a]), ("codex", [b])])
        self.assertEqual((m.agreement, m.by), ((2, 2), ["claude", "codex"]))

    def test_a_finding_only_one_pass_raised_is_one_of_two(self):
        a = F("high", "Guard after loop", "", "src/a.ts")
        c = F("critical", "Token logged", "", "src/b.ts")
        merged = {m.finding.title: m for m in review.merge_passes([("claude", [a]), ("codex", [c])])}
        self.assertEqual(merged["Guard after loop"].agreement, (1, 2))
        self.assertEqual(merged["Token logged"].by, ["codex"])

    def test_the_higher_severity_wins_when_they_differ(self):
        a = F("medium", "Guard after loop", "", "src/a.ts")
        b = F("critical", "Guard after loop", "", "src/a.ts")
        (m,) = review.merge_passes([("claude", [a]), ("codex", [b])])
        self.assertEqual(m.finding.severity, "critical")

    def test_one_pass_has_no_agreement_to_report(self):
        (m,) = review.merge_passes([("claude", [F("low", "t", "", None)])])
        self.assertIsNone(m.agreement)

    def test_the_same_agent_twice_is_told_apart(self):
        a = F("high", "t", "", "p")
        (m,) = review.merge_passes([("claude", [a]), ("claude", [a])])
        self.assertEqual(m.by, ["claude #1", "claude #2"])


class Worth(unittest.TestCase):
    def test_only_critical_or_high_asks_for_a_second_opinion(self):
        self.assertTrue(review.wants_second_opinion([F("high", "t")]))
        self.assertTrue(review.wants_second_opinion([F("low", "a"), F("critical", "b")]))
        self.assertFalse(review.wants_second_opinion([F("medium", "t"), F("idea", "u")]))
        self.assertFalse(review.wants_second_opinion([]))

    def test_the_second_pass_never_costs_more_than_the_first(self):
        self.assertEqual(review.second_cap(first=0.6, cap=2.0, left=9.0), 0.6)

    def test_nor_more_than_the_per_review_cap(self):
        self.assertEqual(review.second_cap(first=3.0, cap=2.0, left=9.0), 2.0)

    def test_nor_more_than_is_left_today(self):
        self.assertEqual(review.second_cap(first=1.0, cap=2.0, left=0.4), 0.4)

    def test_too_little_left_means_no_second_pass(self):
        self.assertIsNone(review.second_cap(first=1.0, cap=2.0, left=0.02))
        self.assertIsNone(review.second_cap(first=0.0, cap=2.0, left=5.0))


class Plugin(unittest.TestCase):
    def plugin(self, spent=0.0, others=("codex",)):
        p = reviewer.Plugin.__new__(reviewer.Plugin)
        p.lock = threading.RLock()
        p.settings = {"maxUsdPerDay": 10, "maxUsdPerReview": 2}
        p.state = {"spend": {reviewer.date.today().isoformat(): spent}}
        p.other_agents = lambda me: list(others)
        return p

    def test_a_clean_first_pass_costs_no_second_one(self):
        ran = []
        p = self.plugin()
        got = p.second_opinion(lambda aid, cap: ran.append(aid), "claude", out([F("medium", "t")]))
        self.assertEqual(ran, [])
        self.assertEqual(got, (None, 0.0))

    def test_a_high_finding_runs_one_pass_with_another_agent(self):
        ran = []
        second = out([F("high", "t", "", "p")], cost=0.3)
        p = self.plugin()
        merged, extra = p.second_opinion(lambda aid, cap: ran.append((aid, cap)) or second, "claude", out([F("high", "t", "", "p")], cost=0.5))
        self.assertEqual(ran, [("codex", 0.5)])
        self.assertEqual(extra, 0.3)
        self.assertEqual(merged[0].agreement, (2, 2))

    def test_with_no_other_agent_the_same_one_runs_again(self):
        ran = []
        p = self.plugin(others=())
        p.second_opinion(lambda aid, cap: ran.append(aid) or out([]), "claude", out([F("high", "t")]))
        self.assertEqual(ran, ["claude"])

    def test_a_failed_second_pass_keeps_the_first_as_it_was(self):
        p = self.plugin()
        merged, extra = p.second_opinion(lambda aid, cap: out([], cost=0.2, ok=False), "claude", out([F("high", "t")]))
        self.assertIsNone(merged)
        self.assertEqual(extra, 0.2)  # what it spent is still charged

    def test_no_second_pass_when_the_day_has_nothing_left(self):
        ran = []
        p = self.plugin(spent=9.99)
        self.assertEqual(p.second_opinion(lambda a, c: ran.append(a), "claude", out([F("critical", "t")])), (None, 0.0))
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
