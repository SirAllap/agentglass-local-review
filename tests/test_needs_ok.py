"""A run that costs more than the person set, or that nobody asked for by label
or button, waits for their OK before the agent starts. No network, no model."""
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


class Rule(unittest.TestCase):
    def test_the_threshold_defaults_to_a_tenth_of_the_days_budget(self):
        self.assertAlmostEqual(review.ask_threshold(None, 10), 1.0)
        self.assertAlmostEqual(review.ask_threshold("", 20), 2.0)

    def test_a_threshold_the_person_set_wins(self):
        self.assertAlmostEqual(review.ask_threshold(0.5, 10), 0.5)

    def test_a_threshold_that_is_not_a_number_falls_back(self):
        self.assertAlmostEqual(review.ask_threshold("lots", 10), 1.0)

    def test_label_and_button_under_the_threshold_start_without_asking(self):
        self.assertFalse(review.must_ask(0.9, 1.0, "label"))
        self.assertFalse(review.must_ask(1.0, 1.0, "button"))

    def test_over_the_threshold_asks_whoever_started_it(self):
        self.assertTrue(review.must_ask(1.01, 1.0, "label"))
        self.assertTrue(review.must_ask(1.01, 1.0, "button"))

    def test_anything_else_asks_even_when_cheap(self):
        self.assertTrue(review.must_ask(0.01, 1.0, "list"))


class Gate(unittest.TestCase):
    def test_a_needs_ok_from_the_gate_stops_the_review_before_the_agent(self):
        ctx = {"title": "t", "author": "a", "head": "h", "base": "b", "body": "", "commits": [], "threads": []}
        pr = github.Pr(repo="acme/orbit", number=7, title="t", author="a", head="a" * 40, base_ref="main", url="", draft=False)
        est = review.estimate(40 * KB, [], 5.0)
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(review.github, "context", return_value=ctx), \
             mock.patch.object(review.github, "diff", return_value="x" * 3000), \
             mock.patch.object(review.github, "snapshot", return_value=0), \
             mock.patch.object(review.cage, "build", side_effect=AssertionError("the agent ran")):
            out = review.run(pr, cage.ADAPTERS["claude"], "", 1.0, None, [], Path(d), gate=lambda n: review.NeedsOk(est))
        self.assertFalse(out.ok)
        self.assertIs(out.needs_ok, est)
        self.assertFalse(out.transport)


def make_plugin(**settings):
    p = reviewer.Plugin.__new__(reviewer.Plugin)
    p.lock = threading.RLock()
    p.settings = {"maxUsdPerDay": 10, "maxUsdPerReview": 2, **settings}
    p.state = {"spend": {}, "history": [], "reviewed": {}, "manual": [], "awaiting": {}}
    p.queue = reviewer.queue.Queue()
    p.queued, p.dropped, p.prs, p.manual = set(), set(), {}, {}
    p.current = None
    p.notice = None
    p.ok_supported = True
    return p


def a_pr(head="a" * 40):
    return github.Pr(repo="acme/orbit", number=7, title="t", author="a", head=head, base_ref="main", url="", draft=False)


class Plugin(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answer = {"ok": True}
        self.patches = [
            mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: self.calls.append((path, body)) or self.answer),
            mock.patch.object(reviewer, "save_state"),
            mock.patch.object(reviewer.Plugin, "draw"),
            mock.patch.object(reviewer.github, "view", side_effect=lambda repo, n: a_pr()),
        ]
        for x in self.patches:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in self.patches])

    def runs(self):
        return [b for path, b in self.calls if path == "/plugin/self/pr/run"]

    def test_the_gate_asks_when_the_high_end_is_over_the_threshold(self):
        p = make_plugin(askAboveUsd=0.05)
        stop = p.ask_gate(40 * KB, "claude", via="button", approved=False)
        self.assertIsInstance(stop, review.NeedsOk)

    def test_the_gate_lets_a_cheap_button_run_start(self):
        p = make_plugin(askAboveUsd=50)
        self.assertIsNone(p.ask_gate(KB, "claude", via="button", approved=False))

    def test_an_approved_run_is_never_asked_twice(self):
        p = make_plugin(askAboveUsd=0.01)
        self.assertIsNone(p.ask_gate(40 * KB, "claude", via="list", approved=True))

    def test_an_older_app_that_cannot_hold_a_run_gets_it_started(self):
        p = make_plugin(askAboveUsd=0.01)
        p.ok_supported = False
        self.assertIsNone(p.ask_gate(40 * KB, "claude", via="list", approved=False))

    def hold(self, p, job):
        est = review.estimate(40 * KB, [], 2.0)
        run = job.run(state="running")
        return p.hold_for_ok(job, run, est)

    def test_a_held_run_is_posted_as_needs_ok_with_its_cost(self):
        p = make_plugin()
        job = reviewer.Job(pr=a_pr(), why="asked for", run_id="r1", via="list")
        self.assertTrue(self.hold(p, job))
        last = self.runs()[-1]
        self.assertEqual(last["state"], "needs-ok")
        self.assertEqual(last["id"], "r1")
        self.assertGreater(last["costUsd"], 0)
        self.assertIn("r1", p.state["awaiting"])

    def test_an_app_that_refuses_needs_ok_is_remembered_and_the_run_goes_on(self):
        p = make_plugin()
        self.answer = {"ok": False, "status": 400}
        job = reviewer.Job(pr=a_pr(), why="asked for", run_id="r1", via="list")
        self.assertFalse(self.hold(p, job))
        self.assertFalse(p.ok_supported)
        self.assertNotIn("r1", p.state["awaiting"])

    def test_approve_queues_the_same_run_already_approved(self):
        p = make_plugin()
        p.prs["acme/orbit#7"] = a_pr()
        job = reviewer.Job(pr=a_pr(), why="asked for", run_id="r1", via="list")
        self.hold(p, job)
        p.run_approve({"id": "r1", "repo": "acme/orbit", "number": 7})
        queued = p.queue.get_nowait()
        self.assertEqual((queued.run_id, queued.approved), ("r1", True))
        self.assertNotIn("r1", p.state["awaiting"])

    def test_skip_cancels_the_run_and_does_not_retry_that_head(self):
        p = make_plugin()
        p.prs["acme/orbit#7"] = a_pr()
        job = reviewer.Job(pr=a_pr(), why="asked for", run_id="r1", via="list")
        self.hold(p, job)
        p.run_skip({"id": "r1", "repo": "acme/orbit", "number": 7})
        self.assertEqual(self.runs()[-1]["state"], "cancelled")
        self.assertTrue(p.queue.empty())
        self.assertEqual(p.state["reviewed"]["acme/orbit#7"]["failed_sha"], "a" * 40)
        self.assertNotIn("tries", p.state["reviewed"]["acme/orbit#7"])

    def test_an_event_for_a_run_it_does_not_hold_changes_nothing(self):
        p = make_plugin()
        p.run_approve({"id": "ghost", "repo": "acme/orbit", "number": 7})
        p.run_skip({"id": "ghost", "repo": "acme/orbit", "number": 7})
        self.assertTrue(p.queue.empty())
        self.assertEqual(self.calls, [])

    def test_a_held_head_is_not_queued_again_by_the_next_scan(self):
        p = make_plugin()
        job = reviewer.Job(pr=a_pr(), why="label", run_id="r1", via="label")
        self.hold(p, job)
        p.enqueue(reviewer.Job(pr=a_pr(), why="label", via="label"))
        self.assertTrue(p.queue.empty())

    def test_a_new_push_takes_back_the_run_that_was_waiting(self):
        p = make_plugin()
        job = reviewer.Job(pr=a_pr(), why="label", run_id="r1", via="label")
        self.hold(p, job)
        p.enqueue(reviewer.Job(pr=a_pr("b" * 40), why="new commits", via="label"))
        states = [(r["id"], r["state"]) for r in self.runs()]
        self.assertIn(("r1", "cancelled"), states)
        self.assertNotIn("r1", p.state["awaiting"])
        self.assertFalse(p.queue.empty())


if __name__ == "__main__":
    unittest.main()
