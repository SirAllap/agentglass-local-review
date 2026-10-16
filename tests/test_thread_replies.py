"""The plugin answers a finding's thread after a new push: fixed, still there,
or not raised again in the changes. Never to GitHub. No network, no model."""
import sys
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import github  # noqa: E402
import review  # noqa: E402
import reviewer  # noqa: E402

SHA = "3f9a2c1" + "0" * 33


class Words(unittest.TestCase):
    def test_a_whole_review_that_no_longer_finds_it_says_fixed(self):
        self.assertEqual(review.thread_answer(False, SHA, whole=True), "Checked on 3f9a2c1: fixed.")

    def test_a_whole_review_that_finds_it_again_says_so(self):
        self.assertEqual(review.thread_answer(True, SHA, whole=True), "Checked on 3f9a2c1: still there.")

    def test_a_review_of_only_the_changes_never_claims_it_is_fixed(self):
        text = review.thread_answer(False, SHA, whole=False)
        self.assertNotIn("fixed", text)
        self.assertIn("changes since", text)

    def test_an_hour_allows_at_most_what_the_app_does_minus_a_margin(self):
        now = 10_000_000
        recent = [now - 1000] * 25
        self.assertEqual(review.replies_left(recent, now), 0)
        self.assertEqual(review.replies_left([now - 3_700_000] * 25, now), 25)
        self.assertEqual(review.replies_left([], now), 25)


def a_pr():
    return github.Pr(repo="acme/orbit", number=7, title="t", author="a", head=SHA, base_ref="main", url="", draft=False)




class Plugin(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answer = {"ok": True}
        self.p = reviewer.Plugin.__new__(reviewer.Plugin)
        p = self.p
        p.lock = threading.RLock()
        p.state = {"reviewed": {"acme/orbit#7": {"asked": {"f-1": 1, "f-2": 2}}}, "history": [], "spend": {}}
        p.reply_supported = True
        patches = [mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: self.calls.append((path, body)) or self.answer),
                   mock.patch.object(reviewer, "save_state"),
                   mock.patch.object(reviewer.time, "time", return_value=1000.0)]
        for x in patches:
            x.start()
            self.addCleanup(x.stop)

    def replies(self):
        return [b for path, b in self.calls if path == "/plugin/self/pr/notes/reply"]

    def test_an_answered_thread_is_answered_once(self):
        self.p.answer_threads(a_pr(), {"f-2"}, whole=True)
        got = {r["id"]: r["text"] for r in self.replies()}
        self.assertIn("fixed", got["f-1"])
        self.assertIn("still there", got["f-2"])
        self.assertEqual(self.p.state["reviewed"]["acme/orbit#7"]["asked"], {})
        self.p.answer_threads(a_pr(), set(), whole=True)
        self.assertEqual(len(self.replies()), 2)

    def test_a_closed_finding_is_not_answered(self):
        self.p.state["reviewed"]["acme/orbit#7"]["closed"] = [{"id": "f-1", "status": "resolved"}]
        self.p.answer_threads(a_pr(), set(), whole=True)
        self.assertEqual([r["id"] for r in self.replies()], ["f-2"])

    def test_the_hourly_allowance_is_kept(self):
        self.p.state["replyTimes"] = [999_000] * 25
        self.p.answer_threads(a_pr(), set(), whole=True)
        self.assertEqual(self.replies(), [])
        self.assertEqual(len(self.p.state["reviewed"]["acme/orbit#7"]["asked"]), 2)  # kept for the next review

    def test_an_app_without_the_route_is_not_asked_again(self):
        self.answer = {"ok": False, "status": 404}
        self.p.answer_threads(a_pr(), set(), whole=True)
        self.assertEqual(len(self.replies()), 1)
        self.assertFalse(self.p.reply_supported)

    def test_an_unknown_finding_is_dropped_and_the_rest_go_on(self):
        answers = iter([{"ok": False, "status": 404, "error": "no such finding"}, {"ok": True}])
        with mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: self.calls.append((path, body)) or next(answers)):
            self.p.answer_threads(a_pr(), set(), whole=True)
        self.assertEqual(len(self.replies()), 2)
        self.assertTrue(self.p.reply_supported)
        self.assertEqual(self.p.state["reviewed"]["acme/orbit#7"]["asked"], {})

    def test_too_many_replies_stops_for_now_and_keeps_the_rest(self):
        self.answer = {"ok": False, "status": 429, "error": "at most 30"}
        self.p.answer_threads(a_pr(), set(), whole=True)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.p.state["reviewed"]["acme/orbit#7"]["asked"]), 2)

    def test_a_person_reply_marks_the_finding_as_asking(self):
        ev = {"repo": "acme/orbit", "number": 7, "id": "f-9", "status": "open", "reply": {"id": "r1", "text": "fixed?", "at": 5, "by": "user"}}
        self.p.note_status(ev)
        self.assertIn("f-9", self.p.state["reviewed"]["acme/orbit#7"]["asked"])

    def test_the_plugins_own_reply_does_not_ask_for_an_answer(self):
        ev = {"repo": "acme/orbit", "number": 7, "id": "f-9", "status": "open", "reply": {"id": "r1", "text": "x", "at": 5, "by": "agent:local-review"}}
        self.p.note_status(ev)
        self.assertNotIn("f-9", self.p.state["reviewed"]["acme/orbit#7"]["asked"])

    def test_closing_a_finding_stops_waiting_for_an_answer(self):
        self.p.note_status({"repo": "acme/orbit", "number": 7, "id": "f-1", "status": "resolved"})
        self.assertNotIn("f-1", self.p.state["reviewed"]["acme/orbit#7"]["asked"])


if __name__ == "__main__":
    unittest.main()
