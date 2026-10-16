"""The app's pr-changed event: check that one pull request at once instead of
waiting for the next scan. No network."""
import sys
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import github  # noqa: E402
import reviewer  # noqa: E402

HEAD = "a" * 40
NEW = "b" * 40


def pr(head=HEAD, draft=False, author="alice"):
    return github.Pr(repo="acme/orbit", number=7, title="t", author=author, head=head, base_ref="main", url="", draft=draft)


def ev(**kw):
    return {"type": "pr-changed", "repo": "acme/orbit", "number": 7, "head": HEAD, "labels": ["agentglass-review"],
            "added": [], "removed": [], "headChanged": False, "at": 1792190000000, **kw}


class Event(unittest.TestCase):
    def setUp(self):
        self.calls = []
        p = self.p = reviewer.Plugin.__new__(reviewer.Plugin)
        p.lock = threading.RLock()
        p.settings = {"onlyMine": True, "repos": ["acme/orbit"]}
        p.state = {"reviewed": {}, "spend": {}, "history": [], "manual": [], "awaiting": {}, "told": {}}
        p.me, p.me_error, p.repos_error = "alice", None, None
        p.queue = reviewer.queue.Queue()
        p.queued, p.dropped, p.prs, p.manual, p.skipped = set(), set(), {}, {}, {}
        p.current, p.notice, p.pr_error = None, None, None
        self.view = mock.patch.object(reviewer.github, "view", side_effect=lambda r, n: pr())
        for x in [mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: self.calls.append((path, body)) or {"ok": True}),
                  mock.patch.object(reviewer, "save_state"), mock.patch.object(reviewer.Plugin, "draw"), self.view,
                  mock.patch.object(reviewer.Plugin, "scan", side_effect=AssertionError("a full scan"))]:
            x.start()
            self.addCleanup(x.stop)

    def runs(self):
        return [b for path, b in self.calls if path == "/plugin/self/pr/run"]

    def test_an_added_label_queues_that_pull_request_without_a_scan(self):
        self.p.pr_changed(ev(added=["agentglass-review"]))
        job = self.p.queue.get_nowait()
        self.assertEqual((job.pr.key, job.via), ("acme/orbit#7", "label"))

    def test_a_push_to_a_labelled_pull_request_is_reviewed_again_since_the_last_review(self):
        self.p.state["reviewed"]["acme/orbit#7"] = {"sha": HEAD}
        self.view.stop()
        with mock.patch.object(reviewer.github, "view", return_value=pr(NEW)):
            self.p.pr_changed(ev(head=NEW, headChanged=True))
        job = self.p.queue.get_nowait()
        self.assertEqual((job.pr.head, job.full, job.why), (NEW, False, "new commits"))

    def test_the_same_head_already_reviewed_is_left_alone(self):
        self.p.state["reviewed"]["acme/orbit#7"] = {"sha": HEAD}
        self.p.pr_changed(ev(added=["agentglass-review"]))
        self.assertTrue(self.p.queue.empty())

    def test_a_draft_gets_its_reason_in_the_panel_and_on_the_pull_request(self):
        self.view.stop()
        with mock.patch.object(reviewer.github, "view", return_value=pr(draft=True)):
            self.p.pr_changed(ev(added=["agentglass-review"]))
        self.assertIn("draft", self.p.skipped["acme/orbit#7"].lower())
        self.assertEqual(self.runs()[-1]["state"], "cancelled")

    def test_a_repository_that_is_not_watched_is_ignored(self):
        self.p.pr_changed(ev(repo="acme/other", added=["agentglass-review"]))
        self.assertTrue(self.p.queue.empty())
        self.assertEqual(self.calls, [])

    def test_somebody_elses_pull_request_is_ignored_when_only_mine(self):
        self.view.stop()
        with mock.patch.object(reviewer.github, "view", return_value=pr(author="bob")):
            self.p.pr_changed(ev(added=["agentglass-review"]))
        self.assertTrue(self.p.queue.empty())
        self.assertEqual(self.p.skipped, {})

    def test_an_event_without_our_label_does_nothing_to_an_idle_pull_request(self):
        self.p.pr_changed(ev(labels=["bug"], added=["bug"]))
        self.assertTrue(self.p.queue.empty())
        self.assertEqual(self.calls, [])

    def test_a_removed_label_takes_a_queued_run_back(self):
        self.p.queued.add("acme/orbit#7")
        self.p.pr_changed(ev(labels=[], removed=["agentglass-review"]))
        self.assertIn("acme/orbit#7", self.p.dropped)
        self.assertNotIn("acme/orbit#7", self.p.queued)

    def test_a_removed_label_cancels_a_run_waiting_for_an_ok(self):
        self.p.state["awaiting"]["r1"] = {"repo": "acme/orbit", "number": 7, "head": HEAD, "full": False, "why": "label", "via": "label"}
        self.p.pr_changed(ev(labels=[], removed=["agentglass-review"]))
        self.assertEqual(self.p.state["awaiting"], {})
        last = self.runs()[-1]
        self.assertEqual((last["id"], last["state"]), ("r1", "cancelled"))
        self.assertIn("label", last["summary"])

    def test_a_removed_label_leaves_a_run_already_going_alone(self):
        self.p.current = reviewer.Job(pr=pr(), run_id="r2")
        self.p.pr_changed(ev(labels=[], removed=["agentglass-review"]))
        self.assertEqual(self.calls, [])

    def test_a_removed_label_clears_a_reason_that_no_longer_applies(self):
        self.p.skipped["acme/orbit#7"] = "It is a draft."
        self.p.pr_changed(ev(labels=[], removed=["agentglass-review"]))
        self.assertNotIn("acme/orbit#7", self.p.skipped)

    def test_a_malformed_event_is_ignored(self):
        for bad in ({"type": "pr-changed"}, ev(number="x"), ev(labels=None, added=None), ev(repo=5)):
            self.p.pr_changed(bad)
        self.assertTrue(self.p.queue.empty())

    def test_the_handler_routes_the_event(self):
        with mock.patch.object(reviewer.Plugin, "pr_changed") as m:
            self.p.handle(ev())
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
