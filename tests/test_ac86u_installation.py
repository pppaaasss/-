import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from router.ac86u.activate import set_actionable


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class AC86UInstallationTests(unittest.TestCase):
    def test_installer_keeps_state_on_usb_and_does_not_touch_routing(self):
        script = (ROOT / "router/ac86u/install.sh").read_text(encoding="utf-8")
        self.assertIn("/opt/bin/opkg", script)
        self.assertIn("python3 ffprobe", script)
        self.assertIn("openssh-client", script)
        self.assertIn('cru a IPTVHomePrimary "0 2 * * *', script)
        self.assertIn('cru a IPTVHomeRecheck "0 13 * * *', script)
        self.assertNotIn("*/6", script)
        self.assertNotIn("deep_interval_hours", script)
        self.assertIn("home_contract.py", script)
        self.assertIn("home_decision.py", script)
        self.assertIn('"qualified_backup_ttl_hours": 36', script)
        self.assertNotIn('"dead_after_runs"', script)
        self.assertNotIn('"degraded_after_runs"', script)
        self.assertIn('"schedule_timezone": "Asia/Shanghai"', script)
        self.assertIn("IPTV_HOME_PROBE", script)
        self.assertNotIn("GITHUB_TOKEN", script)
        self.assertNotIn("iptables", script)
        self.assertNotIn("nvram set", script)

    def test_receiver_key_is_forced_and_restricted(self):
        script = (ROOT / "scripts/install_home_probe_receiver.sh").read_text(encoding="utf-8")
        self.assertIn('restrict,command=\\"$WRAPPER\\"', script)
        self.assertIn("ssh-ed25519", script)
        self.assertIn("passwd -l", script)
        self.assertNotIn("authorized_keys2", script)

    def test_activation_requires_shadow_window_and_path_confirmation(self):
        now = datetime(2026, 9, 2, 18, 5, tzinfo=UTC).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "state"
            output.mkdir()
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps({
                "output_dir": str(output),
                "upload_enabled": True,
                "actionable": False,
                "route_context": "router-origin-direct-wan",
            }), encoding="utf-8")
            (output / "upload-state.json").write_text(json.dumps({
                "successful_uploads": 4,
                "first_upload_utc": "2026-09-02T00:00:00Z",
                "last_upload_utc": "2026-09-02T18:00:00Z",
            }), encoding="utf-8")
            (output / "latest.json").write_text(json.dumps({
                "generated_utc": "2026-09-02T18:00:00Z",
                "summary": {"circuit_breaker_open": False},
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "verify router-origin"):
                set_actionable(config_path, enabled=True, now_epoch=now)
            value = set_actionable(
                config_path,
                enabled=True,
                confirm_living_room_path=True,
                now_epoch=now,
            )
            self.assertTrue(value["actionable"])
            self.assertEqual("living-room-path-equivalent", value["route_context"])

    def test_deactivation_never_needs_network_or_shadow_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"actionable": True}), encoding="utf-8")
            value = set_actionable(path, enabled=False)
            self.assertFalse(value["actionable"])


if __name__ == "__main__":
    unittest.main()
