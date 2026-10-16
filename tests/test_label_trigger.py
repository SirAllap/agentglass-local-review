"""A labelled pull request is reviewed, held for an OK, or shown with the reason
it is not. Never silent, and not three minutes late. No network."""
import json
import sys
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import github  # noqa: E402
import reviewer  # noqa: E402


def row(n=7, label="agentglass-review"):
    return {"number": n, "labels": [{"name": label}]}


class Fresh(unittest.TestCase):
    """The app answers a list from a cache and refreshes it behind the answer
    (`stale`, `loading`, `fetchedAt`). A pull request labelled a minute ago is
    not in the answer that started the refresh."""

    def run_it(self, answers, t0=1000.0):
        seen, clock = [], [t0]
        it = iter(answers)

        def get(path, params, timeout=60):
            seen.append(dict(params))
            return next(it)

        with mock.patch.object(github, "app_get", side_effect=get):
            out = github.fresh_list("/root", "agentglass-review", now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        return out, seen

    def test_asks_for_a_refresh_and_waits_until_the_answer_is_newer_than_the_ask(self):
        old = {"prs": [], "fetchedAt": 900_000, "loading": True}
        new = {"prs": [row()], "fetchedAt": 1_001_000, "loading": False}
        out, seen = self.run_it([old, old, new])
        self.assertEqual([r["number"] for r in out["prs"]], [7])
        self.assertEqual(seen[0].get("force"), "1")
        self.assertNotIn("force", seen[1])

    def test_gives_up_after_a_while_with_what_it_has(self):
        stuck = {"prs": [row()], "fetchedAt": 1, "loading": True}
        out, seen = self.run_it([stuck] * 40)
        self.assertEqual(out, stuck)
        self.assertLess(len(seen), 40)

    def test_an_older_app_that_ignores_all_this_is_taken_at_its_word(self):
        plain = {"prs": [row()]}
        out, seen = self.run_it([plain])
        self.assertEqual(out, plain)
        self.assertEqual(len(seen), 1)

    def test_no_answer_is_no_answer(self):
        out, _ = self.run_it([None])
        self.assertIsNone(out)


def pr(draft=False, head="a" * 40, n=7):
    return github.Pr(repo="acme/orbit", number=n, title="t", author="alice", head=head, base_ref="main", url="", draft=draft)


def plugin(**settings):
    p = reviewer.Plugin.__new__(reviewer.Plugin)
    p.lock = threading.RLock()
    p.settings = {"onlyMine": False, **settings}
    p.state = {"reviewed": {}, "spend": {}, "history": [], "manual": [], "awaiting": {}, "told": {}}
    p.me, p.me_error, p.repos_error = "alice", None, None
    p.queue = reviewer.queue.Queue()
    p.queued, p.dropped, p.prs, p.manual = set(), set(), {}, {}
    p.current, p.notice, p.pr_error = None, None, None
    p.skipped = {}
    p.last_scan = 0
    p.scanning = threading.Lock()
    return p


class Never(unittest.TestCase):
    def setUp(self):
        self.calls = []
        for x in [mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: self.calls.append((path, body)) or {"ok": True}),
                  mock.patch.object(reviewer, "save_state"), mock.patch.object(reviewer.Plugin, "draw"),
                  mock.patch.object(reviewer.github, "viewer", return_value="alice")]:
            x.start()
            self.addCleanup(x.stop)

    def scan(self, p, prs):
        p.settings["repos"] = ["acme/orbit"]
        with mock.patch.object(reviewer.github, "labeled", return_value=prs):
            p._scan()

    def runs(self):
        return [b for path, b in self.calls if path == "/plugin/self/pr/run"]

    def test_a_labelled_pull_request_is_queued(self):
        p = plugin()
        self.scan(p, [pr()])
        self.assertEqual(p.queue.qsize(), 1)

    def test_a_draft_is_shown_with_the_setting_that_would_review_it(self):
        p = plugin()
        self.scan(p, [pr(draft=True)])
        self.assertTrue(p.queue.empty())
        self.assertIn("Review drafts", p.skipped["acme/orbit#7"])

    def test_the_reason_reaches_the_pull_request_itself_once(self):
        p = plugin()
        self.scan(p, [pr(draft=True)])
        self.scan(p, [pr(draft=True)])
        said = [r for r in self.runs() if r["state"] == "cancelled"]
        self.assertEqual(len(said), 1)
        self.assertIn("Review drafts", said[0]["summary"])
        self.assertEqual(said[0]["sha"], "a" * 40)

    def test_a_new_head_gets_told_again_and_a_fixed_reason_clears(self):
        p = plugin()
        self.scan(p, [pr(draft=True)])
        self.scan(p, [pr(draft=True, head="b" * 40)])
        self.assertEqual(len([r for r in self.runs() if r["state"] == "cancelled"]), 2)
        self.scan(p, [pr(draft=False, head="b" * 40)])
        self.assertNotIn("acme/orbit#7", p.skipped)
        self.assertEqual(p.queue.qsize(), 1)

    def test_an_unknown_login_is_a_reason_too(self):
        p = plugin(onlyMine=True)
        p.me = None
        with mock.patch.object(reviewer.github, "viewer", side_effect=github.GhError("nope")):
            self.scan(p, [pr()])
        self.assertTrue(p.queue.empty())
        self.assertIn("login", p.skipped["acme/orbit#7"].lower())

    def test_a_pull_request_that_is_not_mine_is_not_a_complaint(self):
        p = plugin(onlyMine=True)
        p.me = "bob"
        self.scan(p, [pr()])
        self.assertEqual(p.skipped, {})


class Poll(unittest.TestCase):
    def test_the_fallback_poll_is_a_minute(self):
        s = {x["key"]: x for x in json.loads((HERE / "plugin.json").read_text())["contributes"]["settings"]}
        self.assertEqual(s["pollSeconds"]["default"], 60)


if __name__ == "__main__":
    unittest.main()
