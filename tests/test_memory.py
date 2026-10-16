"""A finding dismissed with a reason is not raised again in that repository,
and the next review is told the reason. No network, no model."""
import sys
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import review  # noqa: E402
import reviewer  # noqa: E402


def event(status="dismissed", reply="Intentional: the cache is rebuilt on boot.", **kw):
    ev = {"type": "note-status", "repo": "acme/orbit", "number": 7, "id": "f-aaaa", "status": status, "at": 1}
    if reply is not None:
        ev["reply"] = {"id": "r1", "text": reply, "at": 1, "by": "user", "postedOnGitHub": False, "withStatus": status}
        ev["replies"] = [ev["reply"]]
    return ev | kw


class Plugin(unittest.TestCase):
    def plugin(self):
        p = reviewer.Plugin.__new__(reviewer.Plugin)
        p.lock = threading.RLock()
        p.state = {"reviewed": {"acme/orbit#7": {"notes": {"f-aaaa": {"title": "Cache never expires", "path": "src/cache.py"}}}},
                   "history": [], "spend": {}}
        patcher = mock.patch.object(reviewer, "save_state")
        patcher.start()
        self.addCleanup(patcher.stop)
        return p

    def test_a_dismissal_with_a_reason_is_kept_for_the_repository(self):
        p = self.plugin()
        p.note_status(event())
        d = p.state["decisions"]["acme/orbit"]["f-aaaa"]
        self.assertEqual(d["reason"], "Intentional: the cache is rebuilt on boot.")
        self.assertEqual((d["title"], d["path"]), ("Cache never expires", "src/cache.py"))

    def test_a_dismissal_without_a_reason_is_not_repository_wide(self):
        p = self.plugin()
        p.note_status(event(reply=None))
        self.assertNotIn("acme/orbit", p.state.get("decisions", {}))

    def test_resolved_is_about_that_code_not_the_repository(self):
        p = self.plugin()
        p.note_status(event(status="resolved", reply="Fixed in 3f9a2c1."))
        self.assertNotIn("acme/orbit", p.state.get("decisions", {}))

    def test_reopening_forgets_it(self):
        p = self.plugin()
        p.note_status(event())
        p.note_status(event(status="open", reply=None))
        self.assertNotIn("f-aaaa", p.state["decisions"].get("acme/orbit", {}))

    def test_the_reason_may_come_from_an_earlier_reply(self):
        p = self.plugin()
        ev = event(reply=None)
        ev["replies"] = [{"id": "r0", "text": "Known limit, tracked elsewhere.", "at": 1, "by": "user", "postedOnGitHub": False}]
        p.note_status(ev)
        self.assertEqual(p.state["decisions"]["acme/orbit"]["f-aaaa"]["reason"], "Known limit, tracked elsewhere.")

    def test_the_memory_has_a_ceiling(self):
        p = self.plugin()
        for i in range(reviewer.DECISIONS_MAX + 5):
            p.state["reviewed"]["acme/orbit#7"]["notes"][f"f-{i:04d}"] = {"title": f"t{i}", "path": "a"}
            p.note_status(event(id=f"f-{i:04d}"))
        self.assertEqual(len(p.state["decisions"]["acme/orbit"]), reviewer.DECISIONS_MAX)


def finding(title, severity="medium", path="src/cache.py"):
    return review.Finding(severity=severity, title=title, path=path)


class Apply(unittest.TestCase):
    def memory(self, f):
        return {f.id: {"title": f.title, "path": f.path, "reason": "Rebuilt on boot.", "status": "dismissed"}}

    def test_a_dismissed_finding_is_not_raised_again(self):
        f = finding("Cache never expires")
        kept, n = review.apply_decisions([f, finding("Other problem", path="x.py")], self.memory(f))
        self.assertEqual([k.title for k in kept], ["Other problem"])
        self.assertEqual(n, 1)

    def test_the_same_problem_worded_differently_still_matches(self):
        f = finding("Cache never expires")
        kept, n = review.apply_decisions([finding("Cache  NEVER expires!")], self.memory(f))
        self.assertEqual((kept, n), ([], 1))

    def test_a_critical_one_comes_back_with_the_reason_attached(self):
        f = finding("Cache never expires")
        kept, n = review.apply_decisions([finding("Cache never expires", "critical")], self.memory(f))
        self.assertEqual(n, 0)
        self.assertIn("Rebuilt on boot.", kept[0].body)

    def test_nothing_remembered_changes_nothing(self):
        fs = [finding("A"), finding("B")]
        self.assertEqual(review.apply_decisions(fs, {}), (fs, 0))


class Lines(unittest.TestCase):
    def test_the_next_review_is_told_the_reason(self):
        lines = review.decision_lines({"f-1": {"title": "Cache never expires", "path": "src/cache.py", "reason": "Rebuilt on boot.", "status": "dismissed"}})
        self.assertEqual(len(lines), 1)
        self.assertIn("src/cache.py", lines[0])
        self.assertIn("Rebuilt on boot.", lines[0])

    def test_a_reason_cannot_break_the_line_format(self):
        lines = review.decision_lines({"f-1": {"title": "t", "path": None, "reason": "line one\nline two", "status": "dismissed"}})
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
