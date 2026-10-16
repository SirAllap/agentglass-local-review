"""A review that failed on the way to GitHub is tried again for the same head;
one that failed in the agent is not. No network, no model."""
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


def entry(**kw):
    return {"failed_sha": "abc", **kw}


class Due(unittest.TestCase):
    def test_a_transport_failure_is_due_after_its_wait(self):
        e = entry(tries=1, retry_at=1000)
        self.assertFalse(reviewer.retry_due(e, "abc", 999))
        self.assertTrue(reviewer.retry_due(e, "abc", 1000))

    def test_a_failure_without_a_retry_time_is_never_due(self):
        # The agent itself failed (model error, budget): a new push or a click only.
        self.assertFalse(reviewer.retry_due(entry(), "abc", 10**12))

    def test_three_tries_is_the_end(self):
        self.assertFalse(reviewer.retry_due(entry(tries=reviewer.RETRY_MAX, retry_at=1), "abc", 10**12))

    def test_another_head_is_not_this_failure(self):
        self.assertFalse(reviewer.retry_due(entry(tries=1, retry_at=1), "def", 10**12))

    def test_each_wait_is_longer(self):
        waits = [reviewer.retry_wait(n) for n in range(1, reviewer.RETRY_MAX)]
        self.assertEqual(waits, sorted(set(waits)))


class Remember(unittest.TestCase):
    def plugin(self):
        p = reviewer.Plugin.__new__(reviewer.Plugin)
        p.lock = threading.RLock()
        p.state = {"reviewed": {}, "history": [], "spend": {}}
        return p

    def fail(self, p, transient, head="abc"):
        pr = github.Pr(repo="acme/orbit", number=7, title="t", author="a", head=head, base_ref="main", url="", draft=False)
        with mock.patch.object(reviewer, "save_state"), mock.patch.object(reviewer.time, "time", return_value=100):
            p.remember(pr, "r1", None, "boom", 0.0, transient=transient)
        return p.state["reviewed"]["acme/orbit#7"]

    def test_transport_failure_schedules_a_retry_and_counts(self):
        p = self.plugin()
        e = self.fail(p, True)
        self.assertEqual(e["tries"], 1)
        self.assertEqual(e["retry_at"], 100_000 + reviewer.retry_wait(1) * 1000)
        self.assertEqual(self.fail(p, True)["tries"], 2)

    def test_third_transport_failure_stops_scheduling(self):
        p = self.plugin()
        for _ in range(reviewer.RETRY_MAX):
            e = self.fail(p, True)
        self.assertNotIn("retry_at", e)

    def test_a_new_head_starts_the_count_again(self):
        p = self.plugin()
        self.fail(p, True)
        self.assertEqual(self.fail(p, True, head="def")["tries"], 1)

    def test_agent_failure_schedules_nothing_and_clears_the_count(self):
        p = self.plugin()
        self.fail(p, True)
        e = self.fail(p, False)
        self.assertNotIn("retry_at", e)
        self.assertNotIn("tries", e)


class Classified(unittest.TestCase):
    def run_with(self, boom):
        pr = github.Pr(repo="acme/orbit", number=7, title="t", author="a", head="abc", base_ref="main", url="", draft=False)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(review.github, "context", side_effect=boom):
            return review.run(pr, cage.ADAPTERS["claude"], "", 1.0, None, [], Path(d))

    def test_a_github_error_is_a_transport_failure(self):
        out = self.run_with(github.GhError("gh: Not Found (HTTP 404)"))
        self.assertFalse(out.ok)
        self.assertTrue(out.transport)

    def test_any_other_error_is_not(self):
        self.assertFalse(self.run_with(RuntimeError("model fell over")).transport)


if __name__ == "__main__":
    unittest.main()
