"""The evals' own checks: every case is well formed, the scorer scores what it
should, and the diff the agent will read really contains the planted change.
Nothing here calls a model; `python3 evals/run.py` does, and costs money."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "evals"))

import review  # noqa: E402
import score  # noqa: E402
import fixtures as cases  # noqa: E402


def f(severity, title, path, line=None, body=""):
    return review.Finding(severity=severity, title=title, path=path, line=line, body=body)


class Cases(unittest.TestCase):
    def setUp(self):
        self.all = cases.load_all()

    def test_there_are_cases_and_at_least_one_is_clean(self):
        self.assertGreaterEqual(len(self.all), 5)
        self.assertTrue(any(not c.expect for c in self.all))

    def test_each_expected_bug_sits_on_a_line_the_diff_adds(self):
        for c in self.all:
            added = cases.added_lines(c)
            for e in c.expect:
                lo, hi = e["lines"]
                self.assertTrue(any(n in added.get(e["path"], set()) for n in range(lo, hi + 1)),
                                f"{c.id}/{e['id']}: lines {lo}-{hi} of {e['path']} are not touched by the change")

    def test_the_diff_is_a_unified_diff_of_the_planted_change(self):
        for c in self.all:
            d = cases.diff_of(c)
            self.assertIn("diff --git", d)
            self.assertRegex(d, r"(?m)^\+[^+]")

    def test_fixtures_hold_no_personal_path_or_address(self):
        for c in self.all:
            for p in c.root.rglob("*"):
                if p.is_file():
                    text = p.read_text()
                    self.assertNotRegex(text, r"/(home|Users)/")
                    self.assertNotRegex(text, r"[\w.]+@[\w.]+\.\w+")


class Scoring(unittest.TestCase):
    EXP = [{"id": "x", "path": "a.py", "lines": [10, 12], "severity_min": "high", "any": ["injection"]}]

    def test_found_by_path_line_and_a_word(self):
        s = score.score([f("high", "SQL injection", "a.py", 11)], self.EXP)
        self.assertEqual((s.found, s.missed), (["x"], []))

    def test_a_few_lines_off_still_counts_but_far_off_does_not(self):
        self.assertEqual(score.score([f("high", "Injection", "a.py", 14)], self.EXP).found, ["x"])
        self.assertEqual(score.score([f("high", "Injection", "a.py", 40)], self.EXP).found, [])

    def test_a_finding_with_no_line_matches_by_path_and_word(self):
        self.assertEqual(score.score([f("high", "Injection risk", "a.py")], self.EXP).found, ["x"])

    def test_the_word_may_be_in_the_body(self):
        self.assertEqual(score.score([f("high", "Unsafe query", "a.py", 11, "this allows injection")], self.EXP).found, ["x"])

    def test_wrong_file_or_no_word_is_a_miss(self):
        self.assertEqual(score.score([f("high", "Injection", "b.py", 11)], self.EXP).missed, ["x"])
        self.assertEqual(score.score([f("high", "Style", "a.py", 11)], self.EXP).missed, ["x"])

    def test_found_but_rated_too_low_is_said(self):
        s = score.score([f("low", "Injection", "a.py", 11)], self.EXP)
        self.assertEqual((s.found, s.underrated), (["x"], ["x"]))

    def test_everything_else_is_noise_and_serious_noise_is_counted(self):
        s = score.score([f("high", "Injection", "a.py", 11), f("critical", "Unrelated", "c.py", 1), f("low", "Nit", "c.py", 2)], self.EXP)
        self.assertEqual(len(s.noise), 2)
        self.assertEqual(s.serious_noise, 1)

    def test_a_clean_case_passes_only_with_no_serious_findings(self):
        self.assertTrue(score.score([f("low", "Nit", "a.py", 1)], []).passed)
        self.assertFalse(score.score([f("high", "Bug!", "a.py", 1)], []).passed)

    def test_a_case_passes_when_everything_expected_is_found_and_rated(self):
        self.assertTrue(score.score([f("high", "Injection", "a.py", 11)], self.EXP).passed)
        self.assertFalse(score.score([f("low", "Injection", "a.py", 11)], self.EXP).passed)
        self.assertFalse(score.score([], self.EXP).passed)


if __name__ == "__main__":
    unittest.main()
