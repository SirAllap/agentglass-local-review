"""GitHub read through the app's own routes first, `gh` second. The app is
the one with the login (a keyring included); a plugin's box has none."""
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import github  # noqa: E402

HEAD = "a" * 40
DETAIL = {
    "number": 7, "title": "Retry the upload", "author": "orbit-dev", "baseRefName": "main", "headRefName": "retry",
    "url": "https://github.com/acme/orbit/pull/7", "isDraft": False, "body": "Retries twice.",
    "commits": [{"oid": "b" * 40, "message": "first"}, {"oid": HEAD, "message": "second"}],
    "threads": [{"path": "a.py", "line": 3, "isResolved": False, "isOutdated": False,
                 "comments": [{"author": "rev", "body": "why\ntwice?"}]}],
    "labels": [{"name": "agentglass-review"}],
}


def routes(**answers):
    """An `app_get` that answers by path; a path not listed is the app saying no."""
    def fake(path, params, timeout=60):
        return answers.get(path)
    return fake


class AppFirst(unittest.TestCase):
    def setUp(self):
        github._roots.clear()
        self.addCleanup(github._roots.clear)

    def test_view_reads_the_pull_request_from_the_app_and_never_calls_gh(self):
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/detail": {"ok": True, "detail": DETAIL}})
        with mock.patch("github.app_get", app), mock.patch("github.gh", side_effect=AssertionError("gh called")):
            pr = github.view("acme/orbit", 7)
        self.assertEqual((pr.head, pr.base_ref, pr.author, pr.draft), (HEAD, "main", "orbit-dev", False))

    def test_a_cut_short_commit_list_does_not_guess_the_head(self):
        det = {**DETAIL, "truncated": {"commits": 100}}
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/detail": {"ok": True, "detail": det}})
        gh_row = '{"number": 7, "headRefOid": "%s", "title": "t"}' % ("c" * 40)
        with mock.patch("github.app_get", app), mock.patch("github.gh", return_value=gh_row):
            self.assertEqual(github.view("acme/orbit", 7).head, "c" * 40)

    def test_no_checkout_means_gh_answers(self):
        gh_row = '{"number": 7, "headRefOid": "%s", "title": "t"}' % ("d" * 40)
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", return_value=gh_row) as g:
            self.assertEqual(github.view("acme/orbit", 7).head, "d" * 40)
        self.assertEqual(g.call_args[0][0][:2], ["pr", "view"])

    def test_context_threads_and_commits_come_from_the_app(self):
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/detail": {"ok": True, "detail": DETAIL}})
        with mock.patch("github.app_get", app), mock.patch("github.gh", side_effect=AssertionError("gh called")):
            c = github.context("acme/orbit", 7)
        self.assertEqual(c["commits"], ["first", "second"])
        self.assertEqual(c["threads"], ["[OPEN] a.py:3 (rev): why twice?"])
        self.assertTrue(c["threads_ok"])

    def test_diff_from_the_app_then_gh(self):
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/diff": {"ok": True, "text": "diff --git"}})
        with mock.patch("github.app_get", app):
            self.assertEqual(github.diff("acme/orbit", 7), "diff --git")
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", return_value="from gh"):
            self.assertEqual(github.diff("acme/orbit", 7), "from gh")

    def test_labeled_keeps_only_the_label_asked(self):
        rows = [{"number": 7, "labels": [{"name": "agentglass-review"}]}, {"number": 8, "labels": [{"name": "wip"}]}]
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/list": {"ok": True, "prs": rows},
                        "/prs/detail": {"ok": True, "detail": DETAIL}})
        with mock.patch("github.app_get", app):
            self.assertEqual([p.number for p in github.labeled("acme/orbit", "agentglass-review")], [7])

    def test_viewer_is_the_apps_login(self):
        with mock.patch("github.app_get", routes(**{"/prs/capability": {"available": True, "login": "orbit-dev"}})), \
                mock.patch("github.gh_json", side_effect=AssertionError("gh called")):
            self.assertEqual(github.viewer(), "orbit-dev")


class NewRoutes(unittest.TestCase):
    def setUp(self):
        github._roots.clear()
        self.addCleanup(github._roots.clear)

    def test_the_head_is_the_apps_head_sha_even_when_commits_are_cut_short(self):
        det = {**DETAIL, "headSha": "e" * 40, "baseSha": "f" * 40, "truncated": {"commits": 100}}
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/detail": {"ok": True, "detail": det}})
        with mock.patch("github.app_get", app), mock.patch("github.gh", side_effect=AssertionError("gh called")):
            self.assertEqual(github.view("acme/orbit", 7).head, "e" * 40)

    def test_a_head_sha_that_is_not_40_hex_is_not_trusted(self):
        det = {**DETAIL, "headSha": "main"}
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/detail": {"ok": True, "detail": det}})
        with mock.patch("github.app_get", app):
            self.assertEqual(github.view("acme/orbit", 7).head, HEAD)

    def test_compare_asks_the_app_for_the_pull_requests_own_range(self):
        seen = {}

        def fake(path, params, timeout=60):
            seen[path] = params
            return {"ok": True, "root": "/work/orbit"} if path == "/prs/locate" else {"ok": True, "text": "range diff"}
        with mock.patch("github.app_get", fake), mock.patch("github.gh", side_effect=AssertionError("gh called")):
            self.assertEqual(github.compare("acme/orbit", "a" * 40, "b" * 40, 7), "range diff")
        self.assertEqual(seen["/prs/compare-diff"], {"root": "/work/orbit", "number": "7", "from": "a" * 40, "to": "b" * 40})

    def test_compare_falls_back_to_gh_and_to_none(self):
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", return_value="from gh") as g:
            self.assertEqual(github.compare("acme/orbit", "a" * 40, "b" * 40, 7), "from gh")
        self.assertIn("repos/acme/orbit/compare/%s...%s" % ("a" * 40, "b" * 40), g.call_args[0][0][-1])
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", side_effect=github.GhError("gone")):
            self.assertIsNone(github.compare("acme/orbit", "a" * 40, "b" * 40, 7))

    def test_labeled_sends_the_label_and_still_checks_it_for_an_older_app(self):
        seen = {}
        rows = [{"number": 7, "labels": [{"name": "agentglass-review"}]}, {"number": 8, "labels": [{"name": "wip"}]}]

        def fake(path, params, timeout=60):
            seen[path] = params
            return {"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/list": {"ok": True, "prs": rows},
                    "/prs/detail": {"ok": True, "detail": DETAIL}}[path]
        with mock.patch("github.app_get", fake):
            self.assertEqual([p.number for p in github.labeled("acme/orbit", "agentglass-review")], [7])
        self.assertEqual(seen["/prs/list"]["label"], "agentglass-review")


class SelfTest(unittest.TestCase):
    def setUp(self):
        github._roots.clear()
        self.addCleanup(github._roots.clear)

    def test_reachable_when_the_app_lists_the_watched_repository(self):
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}, "/prs/list": {"ok": True, "prs": []}})
        with mock.patch("github.app_get", app), mock.patch("github.gh", side_effect=AssertionError("gh called")):
            self.assertIsNone(github.unreachable(["acme/orbit"]))

    def test_unreachable_says_what_the_app_answered_and_the_fix(self):
        def fake(path, params, timeout=60):
            github.app_said = "/prs/locate: no checkout of acme/orbit on this machine"
        with mock.patch("github.app_get", fake), mock.patch("github.gh", side_effect=github.GhError("HTTP 401")):
            why = github.unreachable(["acme/orbit"])
        self.assertIn("no checkout of acme/orbit", why)
        self.assertIn(github.FIX_LOGIN, why)


class AppGet(unittest.TestCase):
    def test_without_a_token_it_says_so_and_does_not_call(self):
        with mock.patch.object(github, "TOKEN", ""), mock.patch("urllib.request.urlopen", side_effect=AssertionError("called")):
            self.assertIsNone(github.app_get("/prs/list", {}))
        self.assertIn("token", github.app_said)

    def test_a_refusal_keeps_the_apps_own_words(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b'{"error": "needs full access"}'))
        with mock.patch.object(github, "TOKEN", "t"), mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertIsNone(github.app_get("/prs/detail", {}))
        self.assertIn("403: needs full access", github.app_said)


if __name__ == "__main__":
    unittest.main()


class NoCheckoutNoLogin(unittest.TestCase):
    """A repository the app has no checkout of, read with a gh that keeps its
    token in a keyring: nothing can read it, and the review says what to do
    instead of failing on a 401 halfway."""

    def setUp(self):
        github._roots.clear()
        github._gh_ok = None
        self.addCleanup(github._roots.clear)

    def test_a_checkout_in_the_app_is_enough(self):
        app = routes(**{"/prs/locate": {"ok": True, "root": "/work/orbit"}})
        with mock.patch("github.app_get", app), mock.patch("github.gh", side_effect=AssertionError("gh called")):
            self.assertIsNone(github.cannot_read("acme/orbit"))

    def test_no_checkout_and_a_gh_that_works_is_enough(self):
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", return_value="{}"):
            self.assertIsNone(github.cannot_read("acme/orbit"))

    def test_neither_says_both_ways_out(self):
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", side_effect=github.GhError("HTTP 401")):
            why = github.cannot_read("acme/orbit")
        self.assertIn("acme/orbit", why)
        self.assertIn("open or clone", why)
        self.assertIn(github.FIX_LOGIN, why)

    def test_gh_is_asked_once_not_per_pull_request(self):
        with mock.patch("github.app_get", routes()), mock.patch("github.gh", side_effect=github.GhError("HTTP 401")) as g:
            github.cannot_read("acme/orbit")
            github.cannot_read("acme/orbit")
        self.assertEqual(g.call_count, 1)
