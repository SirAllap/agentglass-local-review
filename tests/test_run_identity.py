import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer  # noqa: E402


class RunIdentity(unittest.TestCase):
    def test_names_the_agent_and_the_model(self):
        self.assertEqual(reviewer.run_identity("claude", "opus"), {"agent": "claude", "model": "opus"})

    def test_an_empty_model_stays_empty(self):
        self.assertEqual(reviewer.run_identity("claude", "")["model"], "")

    def test_cuts_to_the_lengths_the_app_reads(self):
        out = reviewer.run_identity("a" * 50, "m" * 100)
        self.assertEqual((len(out["agent"]), len(out["model"])), (40, 80))


if __name__ == "__main__":
    unittest.main()
