"""What each agent CLI needs from the cage to start, and what a missing login
says. No network, no model."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tools import Home, cage, make_exe  # noqa: E402


def flag_pairs(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag and i + 2 < len(argv)]


def env_of(argv):
    return {argv[i + 1]: argv[i + 2] for i, a in enumerate(argv) if a == "--setenv"}


class WritableHomeForCliThatLogs(Home):
    """OpenCode writes a log and a database under XDG_DATA_HOME on every start;
    with its folder mounted read-only it died on "FileSystem.open ... .log"."""

    def build(self, name="opencode"):
        make_exe(self.home / ".local/bin" / name)
        (self.home / ".config/opencode").mkdir(parents=True)
        data = self.home / ".local/share/opencode"
        data.mkdir(parents=True)
        (data / "auth.json").write_text("{}")
        snap = self.home / "snap"; snap.mkdir()
        return cage.build(cage.ADAPTERS[name], snap, [], self.home / "out", self.home / "work", "p", "", 1.0)

    def test_its_data_home_is_a_fresh_writable_folder_not_the_real_one(self):
        c = self.build()
        env = env_of(c.argv)
        xdg = self.home / "work" / "xdg"
        self.assertEqual(env["XDG_DATA_HOME"], str(xdg / "data"))
        self.assertIn((str(xdg), str(xdg)), flag_pairs(c.argv, "--bind"))
        real = str(self.home / ".local/share/opencode")
        self.assertNotIn((real, real), flag_pairs(c.argv, "--ro-bind"))

    def test_only_its_login_file_is_taken_from_the_real_folder(self):
        c = self.build()
        real = str(self.home / ".local/share/opencode/auth.json")
        self.assertIn((real, str(self.home / "work/xdg/data/opencode/auth.json")), flag_pairs(c.argv, "--bind"))

    def test_its_config_is_still_read_only(self):
        c = self.build()
        cfg = str(self.home / ".config/opencode")
        self.assertIn((cfg, cfg), flag_pairs(c.argv, "--ro-bind"))


class SignedIn(Home):
    def test_no_login_file_and_no_key_says_so_and_how_to_fix_it(self):
        why = cage.not_signed_in(cage.ADAPTERS["codex"])
        self.assertIn("Codex is not signed in", why)
        self.assertIn("~/.codex/auth.json", why)
        self.assertIn("OPENAI_API_KEY", why)
        self.assertIn("`codex login`", why)

    def test_a_login_file_is_enough(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex/auth.json").write_text("{}")
        self.assertIsNone(cage.not_signed_in(cage.ADAPTERS["codex"]))

    def test_a_key_in_the_environment_is_enough(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertIsNone(cage.not_signed_in(cage.ADAPTERS["codex"]))

    def test_a_cli_without_a_login_file_to_check_is_not_second_guessed(self):
        self.assertIsNone(cage.not_signed_in(cage.ADAPTERS["gemini"]))
        self.assertIsNone(cage.not_signed_in(cage.ADAPTERS["opencode"]))


if __name__ == "__main__":
    unittest.main()
