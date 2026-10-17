"""Explain and Re-check are one short, quiet call about one finding: no run
card, no new findings, no question about money, and the answer in the thread.
No network, no model."""
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

HEAD = "a" * 40
NOTE = {"title": "Guard after loop", "path": "src/a.ts", "body": "The guard runs once the loop is done."}


def a_pr():
    return github.Pr(repo="acme/orbit", number=7, title="t", author="a", head=HEAD, base_ref="main", url="", draft=False)


class FocusedRun(unittest.TestCase):
    def run_it(self, **kw):
        ctx = {"title": "t", "author": "a", "head": "h", "base": "b", "body": "", "commits": ["c1"], "threads": ["[OPEN] x"]}
        seen = {}

        def build(adapter, snap, ro, out, agent, prompt, model, cap):
            seen["cap"] = cap
            raise RuntimeError("stop before the agent")

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(review.github, "context", return_value=ctx), \
             mock.patch.object(review.github, "diff", side_effect=AssertionError("the whole diff was fetched")) as diff, \
             mock.patch.object(review.github, "snapshot", return_value=0), \
             mock.patch.object(review.github, "compare", side_effect=AssertionError("a delta was fetched")), \
             mock.patch.object(review.cage, "build", side_effect=build):
            out = review.run(a_pr(), cage.ADAPTERS["claude"], "", kw.pop("cap", 5.0), "p" * 40, [], Path(d), **kw)
        return out, seen, diff

    def test_a_focused_run_never_fetches_the_whole_diff(self):
        out, seen, diff = self.run_it(focused=True)
        diff.assert_not_called()
        self.assertIn("stop before the agent", out.error)  # got as far as the agent

    def test_the_cap_it_is_given_is_the_cap_the_agent_gets(self):
        _, seen, _ = self.run_it(focused=True, cap=0.25)
        self.assertEqual(seen["cap"], 0.25)

    def test_a_focus_is_cheap_by_construction(self):
        self.assertLessEqual(review.FOCUS_CAP_USD, 0.5)


def make_plugin(**settings):
    p = reviewer.Plugin.__new__(reviewer.Plugin)
    p.lock = threading.RLock()
    p.settings = {"maxUsdPerDay": 10, "maxUsdPerReview": 2, **settings}
    p.state = {"reviewed": {"acme/orbit#7": {"notes": {"f-1": NOTE}, "sha": "p" * 40}}, "spend": {}, "history": [],
               "manual": [], "awaiting": {}}
    p.queue = reviewer.queue.Queue()
    p.queued, p.dropped, p.prs, p.manual = set(), set(), {}, {}
    p.current, p.notice, p.reply_supported, p.proc = None, None, True, None
    p.cage = {"ok": True}
    p.app_spend = None
    return p


class Quiet(unittest.TestCase):
    def setUp(self):
        self.calls = []
        for x in [mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: self.calls.append((path, body)) or {"ok": True}),
                  mock.patch.object(reviewer, "save_state"), mock.patch.object(reviewer.Plugin, "draw"),
                  mock.patch.object(reviewer.cage.tools, "which", return_value="/bin/claude"),
                  mock.patch.object(reviewer.cage, "not_signed_in", return_value=None),
                  mock.patch.object(reviewer.github, "cannot_read", return_value=None),
                  mock.patch.object(reviewer.Plugin, "refresh_spend")]:
            x.start()
            self.addCleanup(x.stop)
        self.p = make_plugin()

    def posted(self, path):
        return [b for p, b in self.calls if p == path]

    def job(self, mode="explain"):
        return reviewer.Job(pr=a_pr(), full=True, why="asked for", via="button", focus=(mode, "f-1"), title="Explain")

    def test_queuing_a_focus_draws_no_run_card(self):
        self.p.enqueue(self.job())
        self.assertEqual(self.posted("/plugin/self/pr/run"), [])
        self.assertEqual(self.p.queue.qsize(), 1)

    def test_it_never_asks_for_an_ok(self):
        self.assertIsNone(self.p.ask_gate(10_000_000, "claude", via="list", approved=False, focus=True))

    def answer(self, out, mode="explain"):
        with mock.patch.object(reviewer.review, "run", return_value=out) as run:
            self.p.review_focus(self.job(mode))
        return run

    def test_an_explanation_goes_to_the_thread_and_nothing_else(self):
        run = self.answer(review.Outcome(ok=True, summary="It runs late because...", cost_usd=0.04))
        replies = self.posted("/plugin/self/pr/notes/reply")
        self.assertEqual([(r["id"], r["text"]) for r in replies], [("f-1", "It runs late because...")])
        self.assertEqual(self.posted("/plugin/self/pr/run"), [])
        self.assertEqual(self.posted("/plugin/self/pr/notes"), [])
        self.assertTrue(run.call_args.kwargs["focused"])
        self.assertLessEqual(run.call_args.args[3], review.FOCUS_CAP_USD)

    def test_it_does_not_mark_the_head_as_reviewed_or_failed(self):
        self.answer(review.Outcome(ok=True, summary="x", cost_usd=0.04))
        self.answer(review.Outcome(ok=False, error="boom", cost_usd=0.01))
        e = self.p.state["reviewed"]["acme/orbit#7"]
        self.assertEqual(e["sha"], "p" * 40)
        self.assertNotIn("failed_sha", e)
        self.assertEqual(self.p.state["history"], [])

    def test_a_failure_is_said_in_the_thread_not_swallowed(self):
        self.answer(review.Outcome(ok=False, error="the agent ran past 30 minutes"))
        (r,) = self.posted("/plugin/self/pr/notes/reply")
        self.assertIn("the agent ran past 30 minutes", r["text"])

    def test_a_sandbox_that_is_not_safe_is_said_in_the_thread(self):
        self.p.cage = {"ok": False, "why": "no bwrap"}
        with mock.patch.object(reviewer.review, "run", side_effect=AssertionError("ran")):
            self.p.review_focus(self.job())
        (r,) = self.posted("/plugin/self/pr/notes/reply")
        self.assertIn("no bwrap", r["text"])

    def test_the_spend_is_counted_though_no_run_carries_it(self):
        self.p.app_spend = (reviewer.datetime.now(reviewer.timezone.utc).date().isoformat(), 1.0)
        self.answer(review.Outcome(ok=True, summary="x", cost_usd=0.25))
        self.assertAlmostEqual(self.p.spent_today(), 1.25)

    def test_an_approved_leftover_keeps_its_focus(self):
        job = self.job("recheck")
        job.run_id = "r1"
        self.p.hold_for_ok(job, job.run(state="running"), review.estimate(1024, [], 2.0))
        self.p.prs["acme/orbit#7"] = a_pr()
        with mock.patch.object(reviewer.github, "view", return_value=a_pr()):
            self.p.run_approve({"id": "r1", "repo": "acme/orbit", "number": 7})
        self.assertEqual(self.p.queue.get_nowait().focus, ("recheck", "f-1"))


if __name__ == "__main__":
    unittest.main()
