import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HongKongMonitorSafetyTests(unittest.TestCase):
    def test_periodic_cycle_is_read_only(self):
        cycle = (ROOT / "scripts/hk_cycle.sh").read_text(encoding="utf-8")
        self.assertIn("--playlist tv.m3u", cycle)
        self.assertIn("locked production playlists unchanged", cycle)
        self.assertNotIn("hk_auto_update.py", cycle)
        self.assertNotIn("git push", cycle)
        self.assertNotIn("git commit", cycle)
        self.assertNotIn("git add", cycle)

    def test_installer_uses_four_hour_monitoring_schedule(self):
        installer = (ROOT / "scripts/install_hk_probe.sh").read_text(encoding="utf-8")
        self.assertIn("17 */4 * * *", installer)
        self.assertNotIn("17 */3 * * *", installer)
        self.assertIn("Production update: disabled", installer)

    def test_auto_update_policy_stays_frozen(self):
        policy = json.loads((ROOT / "config/hk-auto-update.json").read_text(encoding="utf-8"))
        self.assertFalse(policy["enabled"])
        self.assertEqual(["DEAD"], policy["replace_formal_statuses"])
        self.assertFalse(policy["replace_reachable_routes"])
        self.assertFalse(policy["allow_speed_based_replacement"])
        self.assertFalse(policy["allow_quality_based_replacement"])


if __name__ == "__main__":
    unittest.main()
