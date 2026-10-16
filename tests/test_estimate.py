"""The cost of a review is estimated before the agent runs, from what the
same agent cost on earlier reviews. No network, no model."""
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cage  # noqa: E402
import github  # noqa: E402
import review  # noqa: E402
import reviewer  # noqa: E402

KB = 1024


class Estimate(unittest.TestCase):
    def test_with_history_it_is_the_median_cost_per_kb_times_this_size(self):
        past = [(0.20, 20 * KB), (0.40, 40 * KB), (1.00, 100 * KB)]  # 0.01 $ per KB each
        e = review.estimate(50 * KB, past, cap=5.0)
        self.assertEqual(e.basis, "history")
        self.assertAlmostEqual(e.mid, 0.50, places=2)
        self.assertLess(e.low, e.mid)
        self.assertGreater(e.high, e.mid)

    def test_without_history_it_says_it_is_a_rough_first_guess(self):
        e = review.estimate(40 * KB, [], cap=5.0)
        self.assertEqual(e.basis, "default")
        self.assertGreater(e.mid, 0)

    def test_the_top_of_the_range_never_passes_the_cap_per_review(self):
        e = review.estimate(900 * KB, [(1.0, 10 * KB)], cap=2.0)
        self.assertLessEqual(e.high, 2.0)
        self.assertLessEqual(e.low, e.high)

    def test_a_run_with_no_cost_or_size_is_not_evidence(self):
        e = review.estimate(40 * KB, [(0.0, 10 * KB), (0.3, 0), (0.5, 50 * KB)], cap=5.0)
        self.assertEqual(e.basis, "history")
        self.assertAlmostEqual(e.mid, 0.4, places=2)

    def test_it_reads_as_a_range_in_dollars(self):
        text = review.estimate(40 * KB, [], cap=5.0).text()
        self.assertRegex(text, r"^\$\d+\.\d\d.\$\d+\.\d\d")


class Gate(unittest.TestCase):
    def pr(self):
        return github.Pr(repo="acme/orbit", number=7, title="t", author="a", head="a" * 40, base_ref="main", url="", draft=False)

    def run_review(self, gate):
        ctx = {"title": "t", "author": "a", "head": "h", "base": "b", "body": "", "commits": [], "threads": []}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(review.github, "context", return_value=ctx), \
             mock.patch.object(review.github, "diff", return_value="x" * 3000), \
             mock.patch.object(review.github, "snapshot", return_value=0), \
             mock.patch.object(review.cage, "build", side_effect=AssertionError("the agent ran")):
            return review.run(self.pr(), cage.ADAPTERS["claude"], "", 1.0, None, [], Path(d), gate=gate)

    def test_the_gate_is_told_how_much_the_agent_will_read(self):
        seen = []
        self.run_review(lambda n: seen.append(n) or "stop")
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 3000)

    def test_a_gate_that_says_why_stops_the_review_before_the_agent(self):
        out = self.run_review(lambda n: "too dear")
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "too dear")
        self.assertFalse(out.transport)
        self.assertGreater(out.input_bytes, 3000)


class Budget(unittest.TestCase):
    def plugin(self, spent, day=10, history=()):
        p = reviewer.Plugin.__new__(reviewer.Plugin)
        p.lock = threading.RLock()
        p.settings = {"maxUsdPerDay": day, "maxUsdPerReview": 2}
        p.state = {"spend": {reviewer.date.today().isoformat(): spent}, "history": list(history), "reviewed": {}}
        return p

    def test_a_review_that_cannot_fit_in_what_is_left_today_is_not_started(self):
        p = self.plugin(spent=9.95)
        why = p.budget_gate(self.pr_bytes(), "claude")
        self.assertIn("left today", why)

    def test_a_review_that_fits_is_started(self):
        self.assertIsNone(self.plugin(spent=1.0).budget_gate(self.pr_bytes(), "claude"))

    def pr_bytes(self):
        return 40 * KB

    def test_only_the_same_agents_history_counts(self):
        h = [{"ok": True, "cost": 5.0, "bytes": 10 * KB, "agent": "codex"}]
        p = self.plugin(spent=0, history=h)
        self.assertEqual(p.estimate_for(40 * KB, "claude").basis, "default")
        self.assertEqual(p.estimate_for(40 * KB, "codex").basis, "history")


if __name__ == "__main__":
    unittest.main()
