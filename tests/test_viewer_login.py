"""When the plugin cannot tell who the person is on GitHub, a labelled pull
request is not dropped without a word. No network."""
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


def pr(author="alice"):
    return github.Pr(repo="acme/orbit", number=7, title="t", author=author, head="a" * 40, base_ref="main", url="", draft=False)


def plugin(**settings):
    p = reviewer.Plugin.__new__(reviewer.Plugin)
    p.lock = threading.RLock()
    p.settings = settings
    p.me, p.me_error = None, None
    return p


class Viewer(unittest.TestCase):
    def test_the_app_answers_first(self):
        with mock.patch.object(github, "app_get", return_value={"login": "alice"}):
            self.assertEqual(github.viewer(), "alice")

    def test_when_neither_answers_the_error_says_what_each_said(self):
        github.app_said = "/prs/capability answered 403: nope"
        with mock.patch.object(github, "app_get", return_value=None), \
             mock.patch.object(github, "gh_json", side_effect=github.GhError("gh: Requires authentication (HTTP 401)")):
            with self.assertRaises(github.GhError) as e:
                github.viewer()
        self.assertIn("/prs/capability answered 403", str(e.exception))
        self.assertIn("Requires authentication", str(e.exception))


class Who(unittest.TestCase):
    def test_the_login_the_person_typed_wins_and_asks_nobody(self):
        p = plugin(githubLogin=" Alice ")
        with mock.patch.object(github, "viewer", side_effect=AssertionError("asked")):
            self.assertEqual(p.who_am_i(), "Alice")

    def test_a_login_found_is_kept(self):
        p = plugin()
        with mock.patch.object(github, "viewer", return_value="alice") as v:
            p.who_am_i()
            p.who_am_i()
        self.assertEqual(v.call_count, 1)

    def test_not_knowing_is_said_with_the_fix(self):
        p = plugin()
        with mock.patch.object(github, "viewer", side_effect=github.GhError("both failed")):
            self.assertIsNone(p.who_am_i())
        self.assertIn("both failed", p.me_error)
        self.assertIn("Your GitHub login", p.me_error)

    def test_a_later_success_clears_the_complaint(self):
        p = plugin()
        with mock.patch.object(github, "viewer", side_effect=github.GhError("x")):
            p.who_am_i()
        with mock.patch.object(github, "viewer", return_value="alice"):
            p.who_am_i()
        self.assertIsNone(p.me_error)


class Mine(unittest.TestCase):
    def test_known_login_decides(self):
        p = plugin(onlyMine=True)
        p.me = "alice"
        self.assertTrue(p.is_mine(pr("alice")))
        self.assertFalse(p.is_mine(pr("bob")))

    def test_unknown_login_is_neither_mine_nor_not_mine(self):
        with mock.patch.object(github, "viewer", side_effect=github.GhError("x")):
            self.assertIsNone(plugin(onlyMine=True).is_mine(pr("alice")))

    def test_without_the_filter_everything_is_in(self):
        self.assertTrue(plugin(onlyMine=False).is_mine(pr("bob")))


class RepoListNoise(unittest.TestCase):
    def test_the_same_failure_is_logged_once(self):
        p = plugin()
        p.repos_error = None
        lines = []
        with mock.patch.object(reviewer, "log", side_effect=lambda *a: lines.append(a)), \
             mock.patch.object(github, "accessible_repos", side_effect=github.GhError("gh: Requires authentication (HTTP 401)")):
            p.publish_repos()
            p.publish_repos()
        self.assertEqual(len(lines), 1)

    def test_a_different_failure_is_logged_again(self):
        p = plugin()
        p.repos_error = None
        lines = []
        errs = iter([github.GhError("a"), github.GhError("b")])
        with mock.patch.object(reviewer, "log", side_effect=lambda *a: lines.append(a)), \
             mock.patch.object(github, "accessible_repos", side_effect=lambda: (_ for _ in ()).throw(next(errs))):
            p.publish_repos()
            p.publish_repos()
        self.assertEqual(len(lines), 2)


class Manifest(unittest.TestCase):
    def test_a_setting_for_the_login_exists(self):
        keys = [s["key"] for s in json.loads((HERE / "plugin.json").read_text())["contributes"]["settings"]]
        self.assertIn("githubLogin", keys)


if __name__ == "__main__":
    unittest.main()
