"""The day's spend comes from the app when it keeps one; this plugin's own
count is the fallback. No network."""
import sys
import threading
import unittest
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import reviewer  # noqa: E402

DAY = datetime.now(timezone.utc).date().isoformat()


def plugin(local=1.0):
    p = reviewer.Plugin.__new__(reviewer.Plugin)
    p.lock = threading.RLock()
    p.settings = {}
    p.state = {"spend": {reviewer.date.today().isoformat(): local}}
    p.app_spend = None
    return p


class AppCost(unittest.TestCase):
    def refresh(self, p, answer):
        calls = []
        with mock.patch.object(reviewer, "call", side_effect=lambda m, path, body=None, **k: calls.append(path) or answer):
            p.refresh_spend()
        return calls

    def test_the_apps_number_for_today_wins(self):
        p = plugin(local=1.0)
        calls = self.refresh(p, {"ok": True, "days": [{"plugin": "local-review", "day": DAY, "usd": 3.5, "runs": 4}]})
        self.assertEqual(calls, ["/plugins/pr-notes/cost?plugin=local-review&days=1"])
        self.assertEqual(p.spent_today(), 3.5)

    def test_an_app_without_the_route_leaves_the_local_count(self):
        p = plugin(local=1.0)
        self.refresh(p, {"ok": False, "status": 404})
        self.assertEqual(p.spent_today(), 1.0)

    def test_a_day_with_no_row_yet_is_nothing_spent_there(self):
        p = plugin(local=1.0)
        self.refresh(p, {"ok": True, "days": []})
        self.assertEqual(p.spent_today(), 0.0)

    def test_yesterdays_row_is_not_todays(self):
        p = plugin(local=1.0)
        self.refresh(p, {"ok": True, "days": [{"plugin": "local-review", "day": "2000-01-01", "usd": 9, "runs": 1}]})
        self.assertEqual(p.spent_today(), 0.0)

    def test_a_refresh_that_fails_later_keeps_the_last_good_number(self):
        p = plugin(local=1.0)
        self.refresh(p, {"ok": True, "days": [{"day": DAY, "usd": 2.0}]})
        self.refresh(p, {"ok": False, "status": 0})
        self.assertEqual(p.spent_today(), 2.0)

    def test_a_number_from_another_day_is_not_used(self):
        p = plugin(local=1.0)
        p.app_spend = ("2000-01-01", 7.0)
        self.assertEqual(p.spent_today(), 1.0)

    def test_the_plugin_name_comes_from_the_manifest(self):
        self.assertEqual(reviewer.PLUGIN_NAME, "local-review")


class RunsCarryTheirCost(unittest.TestCase):
    def test_a_finished_run_states_its_cost_so_the_app_can_sum_it(self):
        src = (HERE / "reviewer.py").read_text()
        self.assertIn('"costUsd"', src[src.index('"state": "done"'):src.index('"state": "done"') + 400])


if __name__ == "__main__":
    unittest.main()
