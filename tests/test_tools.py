"""Finding tools when PATH is bare, and the cage starting the real binary.
No network, no model. Run with `python3 -m unittest discover tests`."""
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cage  # noqa: E402
import github  # noqa: E402
import tools  # noqa: E402


def make_exe(p: Path, body: str = "#!/bin/sh\necho ok\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    p.chmod(0o755)
    return p


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.mise = make_exe(self.home / "fakebin" / "mise")
        tools._cache.clear()
        env = {"PATH": "/nonexistent", "HOME": str(self.home)}
        patches = [unittest.mock.patch.dict(os.environ, env, clear=True),
                   unittest.mock.patch.object(tools, "HOME", self.home),
                   unittest.mock.patch.object(tools, "KNOWN_DIRS", ["~/.local/bin"]),
                   unittest.mock.patch.object(cage, "HOME", self.home)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(tools._cache.clear)

    def shim(self, name):
        d = self.home / ".local/share/mise/shims"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).symlink_to(self.mise)
        return d / name


class Find(Home):
    def test_a_tool_in_a_known_place_is_found_with_a_bare_path(self):
        exe = make_exe(self.home / ".local/bin/gh")
        self.assertEqual(tools.which("gh"), str(exe))

    def test_a_mise_shim_on_path_is_never_returned(self):
        self.shim("claude")
        os.environ["PATH"] = str(self.home / ".local/share/mise/shims")
        self.assertIsNone(tools.which("claude"))

    def test_the_newest_mise_install_is_found_past_the_shim(self):
        self.shim("claude")
        os.environ["PATH"] = str(self.home / ".local/share/mise/shims")
        inst = self.home / ".local/share/mise/installs/claude"
        make_exe(inst / "2.1.9/claude")
        new = make_exe(inst / "2.1.10/claude")
        (inst / "latest").symlink_to("./2.1.10")
        self.assertEqual(tools.which("claude"), str(new))

    def test_an_install_with_a_bin_folder_is_found(self):
        exe = make_exe(self.home / ".local/share/mise/installs/gh/2.1.0/gh_2.1.0_linux/bin/gh")
        self.assertEqual(tools.which("gh"), str(exe))

    def test_nothing_found_says_where_it_looked(self):
        found = tools.find("gh")
        self.assertIsNone(found.path)
        self.assertIn("~/.local/bin", found.missing("gh"))
        self.assertIn("installs", found.missing("gh"))

    def test_a_found_path_is_cached(self):
        exe = make_exe(self.home / ".local/bin/gh")
        tools.which("gh")
        os.environ["PATH"] = ""
        with unittest.mock.patch.object(tools, "KNOWN_DIRS", []):
            self.assertEqual(tools.which("gh"), str(exe))


class Gh(Home):
    def test_gh_runs_the_binary_it_found_not_the_bare_name(self):
        exe = make_exe(self.home / ".local/bin/gh", "#!/bin/sh\necho gh-from-known-place\n")
        self.assertEqual(github.gh(["x"]).strip(), "gh-from-known-place")
        self.assertTrue(exe.exists())

    def test_gh_missing_names_where_it_looked(self):
        with self.assertRaises(github.GhError) as cm:
            github.gh(["x"])
        self.assertIn("looked on PATH", str(cm.exception))


class Cage(Home):
    def adapter(self):
        return cage.ADAPTERS["claude"]

    def test_the_cage_runs_the_real_binary_by_absolute_path_not_the_shim(self):
        self.shim("claude")
        os.environ["PATH"] = str(self.home / ".local/share/mise/shims")
        real = make_exe(self.home / ".local/share/mise/installs/claude/2.1.5/claude")
        snap = self.home / "snap"; snap.mkdir()
        c = cage.build(self.adapter(), snap, [], self.home / "out", self.home / "work", "p", "", 1.0)
        self.assertIn(str(real), c.argv)
        self.assertNotIn("claude", [a for a in c.argv if "/" not in a])

    def test_a_symlinked_binary_is_run_by_its_target_which_is_mounted(self):
        target = make_exe(self.home / ".local/share/claude/versions/2.1.7")
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/claude").symlink_to(target)
        snap = self.home / "snap"; snap.mkdir()
        c = cage.build(self.adapter(), snap, [], self.home / "out", self.home / "work", "p", "", 1.0)
        self.assertIn(str(target), c.argv)
        binds = [c.argv[i + 1] for i, a in enumerate(c.argv) if a == "--ro-bind"]
        self.assertIn(str(self.home / ".local/share/claude"), binds)

    def test_only_the_binarys_install_is_mounted_not_home_or_mise_config(self):
        self.shim("claude")
        real = make_exe(self.home / ".local/share/mise/installs/claude/2.1.5/claude")
        (self.home / ".config/mise").mkdir(parents=True)
        mounts = cage.runtime_mounts(self.adapter(), path="")
        self.assertEqual(mounts, [self.home / ".local/share/mise"])
        self.assertTrue(real.exists())

    def test_agent_starts_says_where_it_looked_when_missing(self):
        ok, why = cage.agent_starts(self.adapter())
        self.assertFalse(ok)
        self.assertIn("looked on PATH", why)


if __name__ == "__main__":
    unittest.main()
