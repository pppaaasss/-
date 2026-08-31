import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.dead_only_failover import PLAYLISTS
from scripts.rotate_core_health import rotation_due, run_rotation


UTC = timezone.utc


class CoreHealthRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "harvest").mkdir()
        self.now = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        self.state = self.root / "config/core-health-rotation.json"
        self.health = self.root / "health.json"
        self.failover = self.root / "failover.json"
        self.report = self.root / "rotation/latest.json"
        self.feedback = self.root / "config/home-route-feedback.json"
        self.feedback.write_text(json.dumps({"good": {}, "bad": {}}), encoding="utf-8")
        self.write_state()
        self.failover.write_text(json.dumps({"selected_updates": []}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, **updates):
        state = {
            "enabled": True,
            "interval_days": 3,
            "timezone": "Asia/Singapore",
            "last_completed_local_date": "2026-08-31",
            "verified_pool": "harvest/candidates.jsonl",
            "home_feedback": "config/home-route-feedback.json",
            "policy": {
                "no_replacement_count_limit": True,
                "maximum_health_age_hours": 18,
                "maximum_candidate_age_days": 7,
                "minimum_height_default": 1080,
                "minimum_height_overrides": {"CCTV-4K": 2160},
                "minimum_h264_bitrate_mbps": 5.0,
                "minimum_source_references": 2,
                "minimum_candidate_segment_mbps": 2.0,
                "performance_rotation_enabled": False,
                "home_accepted_routes_are_locked": True,
                "candidate_host_block_after_home_failures": 2,
                "require_known_stream_bitrate_for_quality_upgrade": True,
                "weak_segment_mbps_below": 2.5,
                "weak_startup_seconds_above": 2.0,
            },
        }
        state.update(updates)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def write_case(
        self,
        channels,
        *,
        candidate_height=1080,
        candidate_fps=25.0,
        candidate_bitrate=6.0,
    ):
        playlist = ["#EXTM3U"]
        health_rows = []
        candidates = []
        for index, name in enumerate(channels, 1):
            old = f"http://old{index}.test/live.m3u8"
            new = f"http://new{index}.test/live.m3u8"
            playlist.extend((f"#EXTINF:-1,{name}", old))
            health_rows.append({
                "name": name,
                "url": old,
                "status": "DEGRADED",
                "height": 720,
                "fps": 25.0,
                "segment_mbps": 0.0,
                "startup_s": 0.0,
            })
            candidates.append({
                "name": name,
                "url": new,
                "sources": ["source-a", "source-b"],
                "hk_verified": {
                    "checked_utc": "2026-09-02T12:00:00Z",
                    "segment_ok": True,
                    "width": 1920,
                    "height": candidate_height,
                    "fps": candidate_fps,
                    "codec": "h264",
                    "bitrate_mbps": candidate_bitrate,
                    "stream_mbps": candidate_bitrate,
                    "segment_mbps": 8.0,
                    "startup_s": 0.5,
                },
            })
        text = "\n".join(playlist) + "\n"
        for filename in PLAYLISTS:
            (self.root / filename).write_text(text, encoding="utf-8")
        self.health.write_text(json.dumps({
            "generated_utc": "2026-09-02T23:30:00Z",
            "summary": {"circuit_breaker_open": False},
            "results": health_rows,
        }), encoding="utf-8")
        (self.root / "harvest/candidates.jsonl").write_text(
            "\n".join(json.dumps(row) for row in candidates) + "\n",
            encoding="utf-8",
        )

    def run_case(self, *, apply=True):
        return run_rotation(
            root=self.root,
            state_path=self.state,
            health_path=self.health,
            failover_path=self.failover,
            report_path=self.report,
            now_utc=self.now,
            force=False,
            apply=apply,
        )

    def test_due_after_three_local_calendar_days(self):
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(rotation_due(state, self.now)[0])
        self.assertFalse(rotation_due(state, datetime(2026, 9, 2, tzinfo=UTC))[0])

    def test_one_round_replaces_every_unqualified_channel_without_a_cap(self):
        channels = [f"CCTV-{number}" for number in range(1, 9)]
        self.write_case(channels)
        result = self.run_case()
        self.assertEqual(8, result["replacement_count"])
        self.assertIsNone(result["replacement_limit"])
        for filename in PLAYLISTS:
            text = (self.root / filename).read_text(encoding="utf-8")
            self.assertNotIn("http://old", text)
            self.assertEqual(8, text.count("http://new"))

    def test_cctv4k_never_accepts_a_1080p_spare(self):
        self.write_case(["CCTV-4K"], candidate_height=1080)
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertIn("CCTV-4K", result["unresolved_channels"])

    def test_ambiguous_identity_url_is_rejected(self):
        self.write_case(["CCTV-7"])
        path = self.root / "harvest/candidates.jsonl"
        candidate = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        conflict = dict(candidate)
        conflict["name"] = "四川卫视"
        path.write_text(path.read_text(encoding="utf-8") + json.dumps(conflict) + "\n", encoding="utf-8")
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])

    def test_conservative_mode_keeps_a_good_route_despite_better_remote_speed(self):
        self.write_case(["CCTV-14"], candidate_height=1080, candidate_fps=50.0)
        payload = json.loads(self.health.read_text(encoding="utf-8"))
        payload["results"][0].update({
            "status": "GOOD",
            "height": 1080,
            "fps": 25.0,
            "segment_mbps": 1.5,
            "startup_s": 3.5,
        })
        self.health.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertEqual("healthy", result["decisions"][0]["reason"])

    def test_home_accepted_degraded_route_is_locked(self):
        self.write_case(["CCTV-4"])
        current = "http://old1.test/live.m3u8"
        self.feedback.write_text(json.dumps({
            "good": {"cctv4": [{"url": current}]},
            "bad": {},
        }), encoding="utf-8")
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertEqual("home_accepted_lock", result["decisions"][0]["reason"])

    def test_home_rejected_route_is_still_replaced(self):
        self.write_case(["CCTV-8"])
        current = "http://old1.test/live.m3u8"
        self.feedback.write_text(json.dumps({
            "good": {},
            "bad": {"cctv8": [{"url": current}]},
        }), encoding="utf-8")
        result = self.run_case()
        self.assertEqual(1, result["replacement_count"])
        self.assertEqual("home_feedback_rejected", result["decisions"][0]["reason"])

    def test_repeatedly_home_bad_host_is_not_selected_again(self):
        self.write_case(["CCTV-8"])
        current = "http://old1.test/live.m3u8"
        self.feedback.write_text(json.dumps({
            "good": {},
            "bad": {
                "cctv8": [{"url": current}],
                "cctv2": [{"url": "http://new1.test/failed-a.m3u8"}],
                "cctv4": [{"url": "http://new1.test/failed-b.m3u8"}],
            },
        }), encoding="utf-8")
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertIn("new1.test", result["policy"]["blocked_candidate_hosts"])

    def test_known_low_bitrate_h264_candidate_is_rejected(self):
        self.write_case(["CCTV-10"], candidate_bitrate=3.0)
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertIn("CCTV-10", result["unresolved_channels"])

    def test_unknown_bitrate_cannot_drive_a_quality_upgrade(self):
        self.write_case(["CCTV-10"], candidate_bitrate=0.0)
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertIn("CCTV-10", result["unresolved_channels"])

    def test_not_due_run_does_not_touch_state_or_playlists(self):
        self.write_case(["CCTV-1"])
        self.write_state(last_completed_local_date="2026-09-02")
        before = self.state.read_bytes()
        result = self.run_case()
        self.assertFalse(result["due"])
        self.assertEqual(before, self.state.read_bytes())
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
