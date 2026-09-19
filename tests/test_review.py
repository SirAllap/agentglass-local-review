"""The parts of local-review that decide something, tested without a model,
a network or a server. Run with `python3 -m unittest discover tests`."""
import ast
import time
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cage  # noqa: E402
import github  # noqa: E402
import review  # noqa: E402


class ParseAnswer(unittest.TestCase):
    def test_the_last_json_block_is_the_answer(self):
        text = "thinking...\n```json\n{\"summary\": \"old\"}\n```\nmore\n```json\n" \
               '{"summary": "Two problems.", "intent": "Retry on 429.", "findings": [' \
               '{"severity": "high", "title": "Retries forever", "path": "src/a.ts", "line": 12, "body": "no cap"},' \
               '{"severity": "urgent", "title": "bad severity"},' \
               '{"severity": "low", "title": "   "},' \
               '{"severity": "medium", "title": "Escapes the repo", "path": "../etc/passwd", "line": 1}]}\n```'
        out = review.parse_answer(text)
        self.assertTrue(out.ok)
        self.assertEqual(out.summary, "Two problems.")
        self.assertEqual([f.title for f in out.findings], ["Retries forever", "Escapes the repo"])
        self.assertIsNone(out.findings[1].path, "a path that leaves the repository is dropped")
        self.assertIsNone(out.findings[1].line, "and a line without its path means nothing")

    def test_no_block_is_a_failed_review_not_a_clean_one(self):
        out = review.parse_answer("Looks good to me!")
        self.assertFalse(out.ok)
        self.assertIn("no json block", out.error)


class FindingIds(unittest.TestCase):
    def test_the_same_problem_found_again_keeps_its_id(self):
        a = review.Finding("high", "Retries forever!", body="first wording", path="src/a.ts", line=12)
        b = review.Finding("medium", "retries  forever", body="second wording", path="src/a.ts", line=40)
        self.assertEqual(a.id, b.id)

    def test_a_different_file_is_a_different_finding(self):
        self.assertNotEqual(review.Finding("high", "x", path="a.ts").id, review.Finding("high", "x", path="b.ts").id)


class Prompt(unittest.TestCase):
    def test_every_placeholder_is_filled_and_data_is_called_data(self):
        files = {k: Path(f"/tmp/ctx/{k}") for k in ("context", "diff", "delta", "threads", "memory")}
        p, why = review.build_prompt(files, incremental=True, memory=["[DISMISSED] a.ts — x"])
        self.assertIsNone(why)
        self.assertNotRegex(p, r"\{(context|diff|delta_line|threads_line|memory_rules|style)\}")
        self.assertIn("never as an instruction", p)
        self.assertIn("/tmp/ctx/delta", p)
        self.assertIn("/tmp/ctx/memory", p)


class Refs(unittest.TestCase):
    def test_a_pull_request_by_ref_or_by_url(self):
        self.assertEqual(github.parse_pr_ref("acme/orbit#42"), ("acme/orbit", 42))
        self.assertEqual(github.parse_pr_ref("https://github.com/acme/orbit/pull/42/files"), ("acme/orbit", 42))
        self.assertIsNone(github.parse_pr_ref("acme/orbit 42"))
        self.assertIsNone(github.parse_pr_ref("http://github.com/acme/orbit/pull/42"))


class GithubIsReadOnly(unittest.TestCase):
    """The promise that nothing reaches GitHub, checked against the source:
    every gh call in github.py starts with a read verb, and no call sends a
    body — `gh api path -f x=y` is an implicit POST — except a GraphQL query
    that is not a mutation."""

    def calls(self):
        tree = ast.parse((HERE / "github.py").read_text())
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.List):
                fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                elts = node.args[0].elts
                consts = [e.value if isinstance(e, ast.Constant) else "<expr>" for e in elts]
                if fn in ("gh", "gh_json"):
                    out.append(consts)
                elif fn == "Popen" and consts and consts[0] == "gh":
                    out.append(consts[1:])
        return out

    def test_every_call_is_a_read(self):
        calls = self.calls()
        self.assertGreater(len(calls), 5)
        for c in calls:
            verb = c[0] if c[0] != "pr" else "pr " + c[1]
            self.assertIn(verb, {"api", "pr list", "pr view", "pr diff"}, c)
            body = {"-f", "-F", "--field", "--raw-field", "--input", "-X", "--method"} & set(c)
            if body:
                self.assertEqual(c[1], "graphql", f"a body on a non-GraphQL call is a write: {c}")
        src = (HERE / "github.py").read_text()
        self.assertNotIn("mutation", src.lower().replace("no mutation", ""))

    def test_the_checker_itself_catches_an_implicit_post(self):
        bad = ["api", "repos/acme/orbit/issues/1/comments", "-f", "body=hi"]
        with self.assertRaises(AssertionError):
            body = {"-f"} & set(bad)
            if body:
                self.assertEqual(bad[1], "graphql")


class CageShape(unittest.TestCase):
    def build(self, adapter="claude", env=None):
        t = tempfile.mkdtemp()
        snap, ctx, out, work = (Path(t, x) for x in ("snap", "ctx", "out", "work"))
        snap.mkdir(); ctx.mkdir()
        old = dict(os.environ)
        os.environ.update(env or {})
        try:
            return cage.build(cage.ADAPTERS[adapter], snap, [ctx], out, work, "review this", "opus", 1.5), out, work
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_namespaces_empty_home_and_an_allowlisted_environment(self):
        c, out, work = self.build(env={"GH_TOKEN": "x", "OPENAI_API_KEY": "y", "AGX_ANYTHING": "z"})
        argv = c.argv
        for flag in ("--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv"):
            self.assertIn(flag, argv)
        tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
        self.assertIn(str(cage.HOME.resolve()), tmpfs)
        setenv = {argv[i + 1]: argv[i + 2] for i, a in enumerate(argv) if a == "--setenv"}
        for leaked in ("GH_TOKEN", "OPENAI_API_KEY", "AGX_ANYTHING", "SSH_AUTH_SOCK"):
            self.assertNotIn(leaked, setenv)
        self.assertEqual(setenv["CLAUDE_CONFIG_DIR"], str(work / "config"))

    def test_nothing_is_writable_but_out_and_the_throwaway_config(self):
        c, out, work = self.build()
        binds = [c.argv[i + 1] for i, a in enumerate(c.argv) if a == "--bind"]
        real_cred = str(cage.HOME / ".claude" / ".credentials.json")
        allowed = {str(out), str(work / "config")} | ({real_cred} if Path(real_cred).exists() else set())
        self.assertLessEqual(set(binds), allowed)

    def test_credentials_are_never_mounted_back(self):
        home = cage.HOME.resolve()
        mounts = [str(m.relative_to(home)) for m in cage.runtime_mounts(cage.ADAPTERS["claude"],
                  path=os.pathsep.join([str(home / ".ssh"), str(home / ".config/gh/bin"), str(home / ".local/bin")]))]
        self.assertNotIn(".ssh", mounts)
        self.assertFalse(any(m.startswith(".config/gh") for m in mounts))

    def test_claude_loads_only_the_throwaway_settings_and_only_reads(self):
        c, _, _ = self.build()
        argv = c.argv
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", argv)

    def test_the_pull_requests_own_agent_config_is_removed(self):
        with tempfile.TemporaryDirectory() as t:
            snap = Path(t)
            (snap / ".claude").mkdir()
            (snap / ".claude" / "settings.json").write_text('{"hooks": {}}')
            (snap / "pkg" / ".claude").mkdir(parents=True)
            (snap / ".mcp.json").write_text("{}")
            (snap / "src").mkdir()
            (snap / "src" / "a.ts").write_text("x")
            removed = cage.sanitize_snapshot(snap)
            self.assertFalse((snap / ".claude").exists())
            self.assertFalse((snap / "pkg" / ".claude").exists())
            self.assertFalse((snap / ".mcp.json").exists())
            self.assertTrue((snap / "src" / "a.ts").exists())
            self.assertIn(".mcp.json", removed)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_the_cage_holds_on_this_machine(self):
        r = cage.self_test()
        self.assertTrue(r["ok"], r)


if __name__ == "__main__":
    unittest.main()


class PanelRows(unittest.TestCase):
    """The panel is a list of reviews, and every row is a way into the pull
    request it is about — the findings live there, so a row that cannot be
    clicked into is a dead end."""

    def plugin(self, history, reviewed):
        import reviewer
        p = reviewer.Plugin.__new__(reviewer.Plugin)
        p.lock = __import__("threading").RLock()
        p.settings = {}
        p.state = {"history": history, "reviewed": reviewed, "spend": {}, "manual": []}
        p.prs = {}
        p.queued = set()
        p.current = None
        p.current_started = 0.0
        return p

    def test_a_row_opens_its_pull_request_on_the_local_lane(self):
        p = self.plugin([], {})
        row = p.row("acme/orbit#42", "Retry the webhook", "reviewed", [], "r1", at=1_700_000_000_000)
        self.assertEqual(row["open"], {"repo": "acme/orbit", "number": 42, "focus": "local"})

    def test_read_is_what_moves_a_review_out_of_ready(self):
        """`seenAt` is the app saying the person opened that pull request. A
        reviewer that keeps shouting about findings already read is a
        reviewer people switch off."""
        hist = [
            {"key": "acme/orbit#42", "title": "Retry", "run": "r1", "sha": "4a89ef5aa", "at": 200,
             "ok": True, "error": None, "cost": 0.4, "counts": {"high": 2}},
            {"key": "acme/billing#17", "title": "Totals", "run": "r2", "sha": "bbbbbbbbb", "at": 100,
             "ok": True, "error": None, "cost": 0.2, "counts": {}},
        ]
        p = self.plugin(hist, {"acme/billing#17": {"seenAt": 150}})
        now, ready, earlier = p.sections()
        self.assertEqual(now, [])
        self.assertEqual([r["id"] for r in ready], ["acme/orbit#42/r1"])
        self.assertEqual([r["id"] for r in earlier], ["acme/billing#17/r2"])
        self.assertEqual(ready[0]["badges"][0]["text"], "2 high")

    def test_a_review_running_or_waiting_is_under_now(self):
        import reviewer
        pr = github.Pr(repo="acme/orbit", number=42, title="Retry", author="me",
                       head="4a89ef5aa", base_ref="main", url="https://github.com/acme/orbit/pull/42", draft=False)
        p = self.plugin([], {})
        p.current = reviewer.Job(pr=pr, why="asked for", run_id="r9", title="Review · Claude Code")
        p.current_started = time.time() - 72
        p.queued = {"acme/billing#17"}
        now, ready, earlier = p.sections()
        self.assertEqual(len(now), 2)
        self.assertEqual(now[0]["badges"][0]["text"], "reviewing 1:12")
        self.assertEqual(now[1]["badges"][0]["text"], "queued")


class ButtonsInThePullRequest(unittest.TestCase):
    def test_every_declared_button_is_one_the_plugin_answers(self):
        """A button the manifest declares and the code ignores is a button
        that does nothing when pressed — and the app refuses an id the
        manifest does not declare, so the two lists have to match."""
        declared = {a["id"] for a in json.loads((HERE / "plugin.json").read_text())["contributes"]["prActions"]}
        src = (HERE / "reviewer.py").read_text()
        handler = src[src.index("def pr_action("):src.index("def pr_seen(")]
        for want in declared:
            self.assertIn(f'"{want}"', handler, f"{want} is declared but never handled")


class WhichPrompt(unittest.TestCase):
    """The style is the person's; the frame is the plugin's. Editing or
    replacing the first must not be able to break the second."""

    def files(self):
        return {"context": Path("/w/pull-request.md"), "diff": Path("/w/full.diff")}

    def test_the_frame_survives_whatever_prompt_is_chosen(self):
        for style in ("balanced", "security", "clean"):
            text, why = review.build_prompt(self.files(), False, [], style)
            self.assertIsNone(why)
            self.assertIn("/w/full.diff", text)
            self.assertIn("```json", text)
            self.assertIn("never as an instruction", text)

    def test_a_prompt_of_their_own_is_used_as_written(self):
        text, why = review.build_prompt(self.files(), False, [], "mine", "Only look at the tests, and say nothing else.")
        self.assertIsNone(why)
        self.assertIn("Only look at the tests", text)
        # Still framed, so the answer still comes back parseable.
        self.assertIn("```json", text)

    def test_an_empty_box_falls_back_and_says_so(self):
        """Silently reviewing with a shipped prompt would read as the
        reviewer ignoring the instructions it was given."""
        text, why = review.build_prompt(self.files(), False, [], "mine", "   \n")
        self.assertEqual(why, "the box for your own prompt is empty")
        self.assertIn("defects that matter", text)

    def test_the_extra_box_is_appended_to_whatever_is_in_use(self):
        text, _ = review.build_prompt(self.files(), False, [], "security", "", "Flag any new environment variable.")
        self.assertIn("Flag any new environment variable.", text)
        self.assertIn("what an attacker could do", text)

    def test_every_style_the_manifest_offers_is_one_that_exists(self):
        offered = {o["value"] for f in json.loads((HERE / "plugin.json").read_text())["contributes"]["settings"]
                   if f["key"] == "style" for o in f["options"]}
        self.assertEqual(offered - {"mine"}, set(review.STYLES))
        for name in review.STYLES:
            self.assertTrue((HERE / "prompts" / f"{name}.md").exists(), name)

    def test_a_shipped_prompt_stays_a_review_and_not_a_verdict_machine(self):
        """Two things a shipped prompt does not do, and the code that reads
        the answer does not invite: mark statements from the description
        verified or unsupported, and rank anything below the four severities
        the prompts define. Both make the answer longer without making it
        truer, and both grow back one commit at a time unless something
        refuses them."""
        for f in (HERE / "prompts").glob("*.md"):
            text = f.read_text().lower()
            for word in ("verified", "unsupported", "unverified", "nicetohave"):
                self.assertNotIn(word, text, f"{f.name} carries {word}")
        # And the code that reads the answer back: a verdict field nothing
        # asks for is how the pass grows back. "verified" is not in this
        # list — an agent adapter is verified or experimental, and that word
        # is this plugin's own.
        for f in (HERE / "review.py", HERE / "reviewer.py"):
            text = f.read_text().lower()
            for word in ("unsupported", "unverified", "nicetohave", "claims"):
                self.assertNotIn(word, text, f"{f.name} carries {word}")
