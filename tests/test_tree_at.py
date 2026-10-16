"""The head code comes from the app's checkout when it has one; gh is only
for a repository without a checkout. No network, no gh."""
import io
import sys
import tarfile
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import github  # noqa: E402

HEAD = "a" * 40


def tar_of(files: dict[str, bytes], extra=()) -> bytes:
    """A tar the way the app writes it: rooted at ".", no top directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for ti in extra:
            tf.addfile(ti)
    return buf.getvalue()


class Reply:
    def __init__(self, body: bytes, kind="application/x-tar", sha=HEAD):
        self.body, self.headers = io.BytesIO(body), {"content-type": kind, "x-tree-sha": sha}

    def read(self, n=-1):
        return self.body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FromCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "repo"
        for p in (mock.patch.object(github, "TOKEN", "t"),
                  mock.patch.object(github, "app_root", return_value="/work/orbit"),
                  mock.patch.object(github.subprocess, "Popen", side_effect=AssertionError("gh was used"))):
            p.start()
            self.addCleanup(p.stop)

    def snap(self, reply):
        with mock.patch.object(github.urllib.request, "urlopen", return_value=reply) as u:
            github.snapshot("acme/orbit", HEAD, self.dest, number=7)
        return u

    def test_the_files_come_from_the_app_and_gh_is_not_called(self):
        u = self.snap(Reply(tar_of({"./src/a.py": b"print(1)\n", "./README.md": b"hi"})))
        self.assertEqual((self.dest / "src/a.py").read_text(), "print(1)\n")
        self.assertTrue((self.dest / "README.md").exists())
        url = u.call_args[0][0].full_url
        self.assertIn("/prs/tree-at?", url)
        self.assertIn(f"sha={HEAD}", url)
        self.assertIn("number=7", url)

    def test_an_entry_that_leaves_the_folder_is_skipped_not_fatal(self):
        evil = tarfile.TarInfo("../../escaped")
        evil.size = 0
        self.snap(Reply(tar_of({"./ok.txt": b"1"}, extra=[evil])))
        self.assertTrue((self.dest / "ok.txt").exists())
        self.assertFalse((Path(self.tmp.name).parent / "escaped").exists())

    def test_a_commit_the_checkout_lacks_goes_to_gh(self):
        reply = Reply(b'{"ok": true, "missing": "%s", "error": "that commit is not in this checkout"}' % HEAD.encode(), kind="application/json")
        with self.assertRaises(AssertionError):  # gh was tried
            self.snap(reply)

    def test_a_tree_of_another_commit_is_not_used(self):
        with self.assertRaises(AssertionError):
            self.snap(Reply(tar_of({"./a": b"1"}), sha="b" * 40))
        self.assertFalse((self.dest / "a").exists())

    def test_an_app_that_does_not_answer_goes_to_gh(self):
        with mock.patch.object(github.urllib.request, "urlopen", side_effect=OSError("down")):
            with self.assertRaises(AssertionError):
                github.snapshot("acme/orbit", HEAD, self.dest, number=7)


class NoCheckout(unittest.TestCase):
    def test_gh_is_the_only_way_and_the_app_is_not_asked(self):
        with mock.patch.object(github, "app_root", return_value=None), \
             mock.patch.object(github.urllib.request, "urlopen", side_effect=AssertionError("app asked")), \
             mock.patch.object(github.subprocess, "Popen", side_effect=AssertionError("gh was used")), \
             tempfile.TemporaryDirectory() as d:
            with self.assertRaises(AssertionError) as cm:
                github.snapshot("acme/orbit", HEAD, Path(d) / "r")
        self.assertEqual(str(cm.exception), "gh was used")


if __name__ == "__main__":
    unittest.main()
