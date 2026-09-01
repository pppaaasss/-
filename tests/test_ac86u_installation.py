import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from router.ac86u.activate import confirm_living_room_path, set_actionable
from router.ac86u.home_contract import REPORT_SCHEMA, ROUTE_CONTEXT, url_sha256


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class AC86UInstallationTests(unittest.TestCase):
    def test_installer_keeps_state_on_usb_and_does_not_touch_routing(self):
        script = (ROOT / "router/ac86u/install.sh").read_text(encoding="utf-8")
        self.assertIn("/opt/bin/opkg", script)
        self.assertIn("python3 ffprobe", script)
        self.assertIn("curl git ca-bundle", script)
        self.assertIn("openssh-client", script)
        self.assertIn('cru a IPTVHomePrimary "0 2 * * *', script)
        self.assertIn('cru a IPTVHomeRecheck "0 13 * * *', script)
        self.assertNotIn("*/6", script)
        self.assertNotIn("deep_interval_hours", script)
        self.assertIn("home_contract.py", script)
        self.assertIn("home_decision.py", script)
        self.assertIn("push_home_report.py", script)
        self.assertIn("github_pair.py", script)
        self.assertIn('"qualified_backup_ttl_hours": 36', script)
        self.assertNotIn('"dead_after_runs"', script)
        self.assertNotIn('"degraded_after_runs"', script)
        self.assertIn('"schedule_timezone": "Asia/Shanghai"', script)
        self.assertIn("IPTV_HOME_PROBE", script)
        self.assertNotIn("GITHUB_TOKEN", script)
        self.assertIn('"protected_publishing_ready": False', script)
        self.assertIn("Do not grant write access", script)
        self.assertNotIn("upload_home_report.py", script)
        self.assertNotIn('"upload_host":', script)
        self.assertIn('for old in ("upload_enabled", "upload_host"', script)
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
                "probe_id": "home-ac86u-test",
                "github_push_enabled": True,
                "protected_publishing_ready": True,
                "actionable": False,
                "route_context": "router-origin-direct-wan",
            }), encoding="utf-8")
            current_url = "https://current.test/cctv1.m3u8"
            report = {
                "schema": REPORT_SCHEMA,
                "probe_id": "home-ac86u-test",
                "generated_utc": "2026-09-02T18:00:00Z",
                "run_kind": "recheck-1300",
                "run_status": "COMPLETED",
                "production_modified": False,
                "actionable": False,
                "route_context": ROUTE_CONTEXT,
                "formal_playlist": {
                    "url": "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u",
                    "sha256": "a" * 64,
                    "channel_count": 1,
                },
                "baseline": {
                    "home_network_ok": True,
                    "github_reachable": True,
                    "route_verified": True,
                    "mass_failure_circuit_breaker": False,
                },
                "current_results": [{
                    "channel_key": "cctv1",
                    "name": "CCTV-1",
                    "url": current_url,
                    "url_sha256": url_sha256(current_url),
                    "status": "GOOD",
                    "failure_confirmed": False,
                    "attempt_count": 1,
                    "verification": {
                        "sample_count": 2,
                        "startup_s": 0.8,
                        "min_download_mbps": 20.0,
                        "stream_mbps": 6.0,
                        "headroom_ratio": 3.3,
                        "width": 1920,
                        "height": 1080,
                        "codec": "h264",
                        "fps": 50.0,
                        "bitrate_mbps": 6.0,
                        "deep_checked": True,
                    },
                }],
                "candidate_results": [],
                "decisions": [{
                    "channel_key": "cctv1",
                    "action": "KEEP",
                    "reason": "healthy_home_route",
                    "replacement_candidate_id": None,
                }],
                "summary": {"circuit_breaker_open": False},
            }
            raw = json.dumps(report).encode()
            (output / "latest.json").write_bytes(raw)
            (output / "github-state.json").write_text(json.dumps({
                "successful_reports": 4,
                "first_push_utc": "2026-09-02T00:00:00Z",
                "last_push_utc": "2026-09-02T18:00:00Z",
                "last_report_generated_utc": report["generated_utc"],
                "last_report_sha256": hashlib.sha256(raw).hexdigest(),
                "pending_reports": 0,
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "confirm the Apple TV"):
                set_actionable(config_path, enabled=True, now_epoch=now)
            confirmed = confirm_living_room_path(config_path)
            self.assertFalse(confirmed["actionable"])
            self.assertEqual(ROUTE_CONTEXT, confirmed["route_context"])
            value = set_actionable(config_path, enabled=True, now_epoch=now)
            self.assertTrue(value["actionable"])
            self.assertEqual(ROUTE_CONTEXT, value["route_context"])

    def test_deactivation_never_needs_network_or_shadow_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"actionable": True}), encoding="utf-8")
            value = set_actionable(path, enabled=False)
            self.assertFalse(value["actionable"])


if __name__ == "__main__":
    unittest.main()
