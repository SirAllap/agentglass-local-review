"""Context from the card and from CI goes to the model only for repositories
that turned it on, tells apart a failure the diff may have caused from one
already red elsewhere, and a secret in it stops the review. No network."""
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cage  # noqa: E402
import context  # noqa: E402
import github  # noqa: E402
import review  # noqa: E402

PR = github.Pr(repo="acme/orbit", number=7, title="ORBIT-1042 Retry the upload", author="a", head="a" * 40,
               base_ref="main", url="", draft=False)
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def routes(**answers):
    def fake(path, params, timeout=60):
        a = answers.get(path)
        return a(params) if callable(a) else a
    return fake


class CardRefs(unittest.TestCase):
    def test_ids_are_found_in_title_branch_and_body_once_each(self):
        refs = context.card_refs("ORBIT-1042 Retry", "feature/ORBIT-1042-retry", "Closes ORBIT-1042 and see PLAT-7.")
        self.assertEqual(refs, ["ORBIT-1042", "PLAT-7"])

    def test_nothing_that_looks_like_a_card_gives_nothing(self):
        self.assertEqual(context.card_refs("Fix typo", "fix/typo", "utf-8 and sha-256 are not cards"), [])


class Card(unittest.TestCase):
    def test_a_clickup_card_is_read_by_its_id(self):
        card = {"ok": True, "task": {"title": "Retry the upload", "status": "in review"}, "description": "Retry twice on a 503.",
                "checklists": [{"name": "Done when", "items": [{"name": "backoff", "done": True}]}],
                "comments": [{"who": "pm", "text": "Keep it idempotent."}]}
        with mock.patch.object(github, "app_get", routes(**{"/clickup/task": card})):
            text = context.card(["ORBIT-1042"])
        self.assertIn("Retry the upload", text)
        self.assertIn("Retry twice on a 503.", text)
        self.assertIn("Keep it idempotent.", text)
        self.assertIn("backoff", text)

    def test_jira_is_tried_when_clickup_does_not_know_the_card(self):
        jira = {"ok": True, "task": {"title": "Retry the upload", "status": "To Do"}}
        with mock.patch.object(github, "app_get", routes(**{"/jira/task": jira})):
            self.assertIn("Retry the upload", context.card(["ORBIT-1042"]))

    def test_no_tracker_connected_is_not_an_error(self):
        with mock.patch.object(github, "app_get", routes()):
            self.assertIsNone(context.card(["ORBIT-1042"]))


def job_url(n):
    return f"https://github.com/acme/orbit/actions/runs/9/job/{n}"


class Ci(unittest.TestCase):
    ROLL = {"ok": True, "all": [
        {"name": "unit", "state": "failure", "url": job_url(11)},
        {"name": "lint", "state": "failure", "url": job_url(12)},
        {"name": "e2e", "state": "failure", "url": job_url(13)},
        {"name": "build", "state": "success", "url": job_url(14)},
        {"name": "old", "state": "failure", "cancelled": True, "url": job_url(15)}]}
    CACHED = {"ok": True, "summaries": {
        "11": {"titles": ["test_upload_retries"], "verdicts": [{"kind": "this-pr"}]},
        "12": {"titles": ["E501 line too long"], "verdicts": [{"kind": "main"}]},
        "13": {"titles": ["test_slow_socket"], "verdicts": [{"kind": "flaky"}]}}}

    def text(self, **extra):
        r = routes(**{"/prs/rollup": self.ROLL, "/prs/check-failures-cached": self.CACHED, **extra})
        with mock.patch.object(github, "app_get", r), mock.patch.object(github, "app_root", return_value="/work/orbit"):
            return context.ci("acme/orbit", 7)

    def test_each_failure_says_what_was_seen_about_it(self):
        t = self.text()
        self.assertRegex(t, r"test_upload_retries.*(not seen|only|this pull request)")
        self.assertRegex(t, r"E501 line too long.*default branch")
        self.assertRegex(t, r"test_slow_socket.*(flaky|passed)")

    def test_a_cancelled_check_is_not_a_failure(self):
        self.assertNotRegex(self.text(), r"\bold\b")

    def test_a_green_pull_request_says_so(self):
        green = {"ok": True, "all": [{"name": "unit", "state": "success", "url": job_url(1)}]}
        self.assertIn("no failing", self.text(**{"/prs/rollup": green}).lower())

    def test_no_checkout_means_no_ci_context(self):
        with mock.patch.object(github, "app_root", return_value=None):
            self.assertIsNone(context.ci("acme/orbit", 7))

    def test_a_failure_without_a_read_log_falls_back_to_the_failing_tests_lens(self):
        lens = {"ok": True, "rows": [{"title": "test_upload_retries", "gist": "boom", "check": "unit", "runs": 4, "prs": 3,
                                      "firstSeen": 1, "lastSeen": 2, "verdict": {"kind": "main"}}]}
        t = self.text(**{"/prs/check-failures-cached": {"ok": True, "summaries": {}}, "/prs/failing-tests": lens})
        self.assertRegex(t, r"test_upload_retries.*default branch")


class Secrets(unittest.TestCase):
    def test_a_token_is_named_by_kind_and_never_quoted(self):
        kinds = context.secrets(f"curl -H 'Authorization: token {GITHUB_TOKEN}'")
        self.assertEqual(kinds, ["a GitHub token"])
        self.assertNotIn("ghp_", " ".join(kinds))

    def test_other_shapes(self):
        self.assertTrue(context.secrets("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertTrue(context.secrets("AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertTrue(context.secrets('password = "hunter2hunter2hunter2"'))

    def test_ordinary_text_is_clean(self):
        self.assertEqual(context.secrets("Retry twice on a 503, see commit a1b2c3d and ORBIT-1042."), [])


class Gather(unittest.TestCase):
    def test_a_secret_in_the_card_blocks_and_nothing_is_kept(self):
        card = {"ok": True, "task": {"title": "t"}, "description": f"use {GITHUB_TOKEN}"}
        with mock.patch.object(github, "app_get", routes(**{"/clickup/task": card})), \
             mock.patch.object(github, "app_root", return_value=None):
            g = context.gather(PR, "feature/x", "")
        self.assertEqual(g.blocked, ["a GitHub token"])
        self.assertEqual(g.text, "")

    def test_the_text_is_capped(self):
        card = {"ok": True, "task": {"title": "t"}, "description": "word " * 20000}
        with mock.patch.object(github, "app_get", routes(**{"/clickup/task": card})), \
             mock.patch.object(github, "app_root", return_value=None):
            g = context.gather(PR, "", "")
        self.assertLess(len(g.text), 12000)


class InReview(unittest.TestCase):
    CTX = {"title": "t", "author": "a", "head": "feature/x", "base": "main", "body": "", "commits": [], "threads": []}

    def run_with(self, ctx_fn):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(review.github, "context", return_value=self.CTX), \
             mock.patch.object(review.github, "diff", return_value="diff"), \
             mock.patch.object(review.github, "snapshot", return_value=0), \
             mock.patch.object(review.cage, "build", side_effect=AssertionError("the agent ran")):
            return review.run(PR, cage.ADAPTERS["claude"], "", 1.0, None, [], Path(d), context_fn=ctx_fn)

    def test_without_the_function_the_review_goes_on_to_the_agent(self):
        self.assertIn("the agent ran", self.run_with(None).error)

    def test_a_blocked_review_never_reaches_the_agent(self):
        out = self.run_with(lambda pr, ctx: context.Gathered(text="", blocked=["a GitHub token"]))
        self.assertFalse(out.ok)
        self.assertIn("a GitHub token", out.error)
        self.assertNotIn("ghp_", out.error)
        self.assertFalse(out.transport)

    def test_the_prompt_points_at_the_file_and_calls_it_evidence(self):
        files = {k: Path(f"/tmp/ctx/{k}") for k in ("context", "diff", "extra")}
        p, _ = review.build_prompt(files, False, [])
        self.assertIn("/tmp/ctx/extra", p)
        self.assertNotRegex(p, r"\{extra_line\}")
        p, _ = review.build_prompt({k: v for k, v in files.items() if k != "extra"}, False, [])
        self.assertNotIn("card-and-ci", p)
        self.assertNotRegex(p, r"\{extra_line\}")


if __name__ == "__main__":
    unittest.main()
