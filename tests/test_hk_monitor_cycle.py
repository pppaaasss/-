import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HongKongMonitorSafetyTests(unittest.TestCase):
    def test_periodic_cycle_gates_the_only_production_writer(self):
        cycle = (ROOT / "scripts/hk_cycle.sh").read_text(encoding="utf-8")
        self.assertIn("--playlist tv.m3u", cycle)
        self.assertIn("locked production playlists unchanged", cycle)
        self.assertIn("publish_hk_health.py", cycle)
        self.assertIn("health-monitor", cycle)
        self.assertIn("dead_only_failover.py", cycle)
        self.assertIn("--apply", cycle)
        self.assertIn("confirmed DEAD route replacement", cycle)
        self.assertNotIn("hk_auto_update.py", cycle)
        self.assertLess(cycle.index("dead_only_failover.py"), cycle.index("git add"))

    def test_installer_uses_verified_four_hour_monitoring_schedule(self):
        installer = (ROOT / "scripts/install_hk_probe.sh").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 00/4:17:00", installer)
        self.assertIn("Persistent=true", installer)
        self.assertIn("systemctl is-enabled --quiet iptv-hk-probe.timer", installer)
        self.assertIn("systemctl is-active --quiet iptv-hk-probe.timer", installer)
        self.assertIn("Initial probe or GitHub health upload failed; scheduler not enabled.", installer)
        self.assertIn("17 */4 * * *", installer)
        self.assertNotIn("17 */3 * * *", installer)
        self.assertIn("Production update: confirmed DEAD routes only", installer)

    def test_auto_update_policy_stays_frozen(self):
        policy = json.loads((ROOT / "config/hk-auto-update.json").read_text(encoding="utf-8"))
        self.assertFalse(policy["enabled"])
        self.assertEqual(["DEAD"], policy["replace_formal_statuses"])
        self.assertFalse(policy["replace_reachable_routes"])
        self.assertFalse(policy["allow_speed_based_replacement"])
        self.assertFalse(policy["allow_quality_based_replacement"])

        dead_only = json.loads((ROOT / "config/dead-only-failover.json").read_text(encoding="utf-8"))
        self.assertTrue(dead_only["enabled"])
        self.assertEqual("candidate/tv-core.m3u", dead_only["fixed_candidate_playlist"])
        self.assertEqual(2, dead_only["current_recheck_attempts"])
        self.assertEqual(2, dead_only["candidate_confirm_attempts"])
        self.assertLessEqual(dead_only["maximum_updates_per_cycle"], 3)


if __name__ == "__main__":
    unittest.main()
