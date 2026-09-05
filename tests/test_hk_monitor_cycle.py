import json
import stat
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HongKongMonitorSafetyTests(unittest.TestCase):
    def test_periodic_cycle_self_retires_before_any_probe_or_writer(self):
        cycle = (ROOT / "scripts/hk_cycle.sh").read_text(encoding="utf-8")
        retirement = cycle.index("exit 0")
        self.assertIn("systemctl disable --now iptv-hk-probe.timer", cycle)
        self.assertIn('mv -f "$CRON_FILE" "$CRON_FILE.retired"', cycle)
        self.assertIn("no probe, upload, failover, or production write", cycle)
        self.assertLess(retirement, cycle.index("git fetch"))
        self.assertLess(retirement, cycle.index("hk_probe.py"))
        self.assertLess(retirement, cycle.index("dead_only_failover.py"))
        self.assertLess(retirement, cycle.index("git push origin HEAD:master"))

    def test_legacy_installers_refuse_to_create_hong_kong_paths(self):
        installer = (ROOT / "scripts/install_hk_probe.sh").read_text(encoding="utf-8")
        receiver = (ROOT / "scripts/install_home_probe_receiver.sh").read_text(encoding="utf-8")
        self.assertLess(installer.index("exit 2"), installer.index("apt-get"))
        self.assertLess(receiver.index("exit 2"), receiver.index("while [[ $# -gt 0 ]]"))
        self.assertIn("AC86U is the sole health authority", installer)
        self.assertIn("directly from AC86U", receiver)

    def test_periodic_cycle_is_executable_after_git_checkout(self):
        mode = (ROOT / "scripts/hk_cycle.sh").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_hong_kong_and_three_day_production_policies_are_disabled(self):
        policy = json.loads((ROOT / "config/hk-auto-update.json").read_text(encoding="utf-8"))
        self.assertFalse(policy["enabled"])
        self.assertEqual(["DEAD"], policy["replace_formal_statuses"])
        self.assertFalse(policy["replace_reachable_routes"])
        self.assertFalse(policy["allow_speed_based_replacement"])
        self.assertFalse(policy["allow_quality_based_replacement"])

        dead_only = json.loads((ROOT / "config/dead-only-failover.json").read_text(encoding="utf-8"))
        self.assertFalse(dead_only["enabled"])
        self.assertEqual("home-first-ac86u", dead_only["retired_by"])
        self.assertEqual("candidate/tv-core.m3u", dead_only["fixed_candidate_playlist"])
        self.assertEqual("harvest/pending.jsonl", dead_only["pending_candidate_pool"])
        self.assertTrue(dead_only["dynamic_pending_enabled"])
        self.assertEqual(2, dead_only["current_recheck_attempts"])
        self.assertEqual(2, dead_only["candidate_confirm_attempts"])
        self.assertEqual(0, dead_only["maximum_updates_per_cycle"])

        rotation = json.loads((ROOT / "config/core-health-rotation.json").read_text(encoding="utf-8"))
        self.assertFalse(rotation["enabled"])
        self.assertEqual("home-first-ac86u", rotation["retired_by"])
        self.assertEqual(3, rotation["interval_days"])
        self.assertTrue(rotation["policy"]["no_replacement_count_limit"])

        workflow = (ROOT / ".github/workflows/rotate-core-health.yml").read_text(encoding="utf-8")
        harvest = (ROOT / ".github/workflows/harvest-sources.yml").read_text(encoding="utf-8")
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("cron:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("if: ${{ false }}", workflow)
        self.assertIn("scripts/rotate_core_health.py", workflow)
        self.assertNotIn("schedule:", harvest)
        self.assertNotIn("cron:", harvest)
        self.assertIn("contents: read", harvest)
        self.assertIn("if: ${{ false }}", harvest)


if __name__ == "__main__":
    unittest.main()
