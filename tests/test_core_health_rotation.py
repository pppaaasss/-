import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.dead_only_failover import PLAYLISTS
from scripts.rotate_core_health import playlist_entries, rotation_due, run_rotation


UTC = timezone.utc


class CoreHealthRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "harvest").mkdir()
        (self.root / "candidate").mkdir()
        self.now = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        self.state = self.root / "config/core-health-rotation.json"
        self.health = self.root / "health.json"
        self.failover = self.root / "failover.json"
        self.report = self.root / "rotation/latest.json"
        self.feedback = self.root / "config/home-route-feedback.json"
        self.home = self.root / "home.json"
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
                "maximum_home_health_age_hours": 18,
                "maximum_candidate_age_days": 7,
                "minimum_height_default": 1080,
                "minimum_height_overrides": {"CCTV-4K": 2160},
                "minimum_h264_bitrate_mbps": 5.0,
                "minimum_source_references": 2,
                "minimum_candidate_segment_mbps": 2.0,
                "performance_rotation_enabled": False,
                "live_home_probe_enabled": True,
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
        candidate_playlist = ["#EXTM3U"]
        health_rows = []
        candidates = []
        for index, name in enumerate(channels, 1):
            old = f"http://old{index}.test/live.m3u8"
            new = f"http://new{index}.test/live.m3u8"
            playlist.extend((f"#EXTINF:-1,{name}", old))
            candidate_playlist.extend((f"#EXTINF:-1,{name}", new))
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
        (self.root / "candidate/tv-core.m3u").write_text(
            "\n".join(candidate_playlist) + "\n",
            encoding="utf-8",
        )
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
            home_path=self.home if self.home.exists() else None,
        )

    @staticmethod
    def home_row(name, url, *, status, candidate=False):
        samples = [] if status == "DEAD" else [
            {
                "url": f"{url.rsplit('/', 1)[0]}/{index}.ts",
                "downloaded_bytes": 2097152,
                "total_bytes": 3145728,
                "duration_s": 4.0,
                "elapsed_s": 0.5,
                "download_mbps": 33.554,
                "stream_mbps": 6.291,
                "complete": False,
            }
            for index in (1, 2)
        ]
        row = {
            "name": name,
            "url": url,
            "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
            "status": status,
            "observed_status": "UNKNOWN" if status == "DEAD" else status,
            "sample_count": len(samples),
            "segment_samples": samples,
            "startup_s": 0.8 if samples else 0.0,
            "min_download_mbps": 33.554 if samples else 0.0,
            "avg_download_mbps": 33.554 if samples else 0.0,
            "stream_mbps": 6.291 if samples else 0.0,
            "headroom_ratio": 5.334 if samples else 0.0,
            "width": 1920 if status == "GOOD" else 1280 if status == "DEGRADED" else 0,
            "height": 1080 if status == "GOOD" else 720 if status == "DEGRADED" else 0,
            "codec": "h264" if status != "DEAD" else "",
            "fps": 50.0 if status != "DEAD" else 0.0,
            "bitrate_mbps": 6.291 if status != "DEAD" else 0.0,
            "min_height": 1080,
            "deep_checked": status != "DEAD",
            "error": "confirmed_home_failure" if status == "DEAD" else "",
            "consecutive_failures": 3 if status == "DEAD" else 0,
            "failure_age_hours": 6.0 if status == "DEAD" else 0.0,
            "home_dead_confirmed": status == "DEAD",
            "consecutive_degraded": 3 if status == "DEGRADED" else 0,
            "degraded_age_hours": 6.0 if status == "DEGRADED" else 0.0,
            "home_degraded_confirmed": status == "DEGRADED",
        }
        if candidate:
            row["candidate_confirmed"] = True
        return row

    def write_home_evidence(
        self,
        name,
        *,
        current_status="DEAD",
        actionable=True,
        candidate_confirmed=True,
        route_context="living-room-path-equivalent",
        playlist_sha=None,
    ):
        current = next(url for channel, url in playlist_entries(self.root / "tv-core.m3u") if channel == name)
        candidate = next(url for channel, url in playlist_entries(self.root / "candidate/tv-core.m3u") if channel == name)
        current_row = self.home_row(name, current, status=current_status)
        candidates = [self.home_row(name, candidate, status="GOOD", candidate=True)] if candidate_confirmed else []
        counts = {"GOOD": 0, "DEGRADED": 0, "UNKNOWN": 0, "DEAD": 0}
        counts[current_status] += 1
        report = {
            "schema": "iptv-home-probe/v1",
            "probe_id": "home-ac86u-123",
            "generated_utc": "2026-09-02T23:30:00Z",
            "run_status": "COMPLETED",
            "mode": "deep",
            "actionable": actionable,
            "production_modified": False,
            "probe_region": "home",
            "route_context": route_context,
            "playlist": {
                "url": "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u",
                "sha256": playlist_sha or hashlib.sha256((self.root / "tv-core.m3u").read_bytes()).hexdigest(),
                "channel_count": 1,
            },
            "candidate_playlist": {
                "url": "https://raw.githubusercontent.com/pppaaasss/-/master/candidate/tv-core.m3u",
                "sha256": hashlib.sha256((self.root / "candidate/tv-core.m3u").read_bytes()).hexdigest(),
                "channel_count": 1,
            },
            "policy": {
                "auto_replace_formal_routes": False,
                "mass_failure_circuit_breaker": True,
                "samples_per_route": 2,
                "sample_bytes": 12582912,
            },
            "resources": {"load1": 0.2, "mem_available_kib": 180000, "runtime_s": 40.0},
            "summary": {
                "channels": 1,
                "good": counts["GOOD"],
                "degraded": counts["DEGRADED"],
                "unknown": counts["UNKNOWN"],
                "dead": counts["DEAD"],
                "candidate_channels": len(candidates),
                "candidate_confirmed": len(candidates),
                "circuit_breaker_open": False,
            },
            "results": [current_row],
            "candidate_results": candidates,
            "transport": {
                "via": "ssh-forced-command",
                "receiver_validated": True,
                "received_utc": "2026-09-02T23:31:00Z",
                "report_sha256": "b" * 64,
            },
        }
        self.home.write_text(json.dumps(report), encoding="utf-8")

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

    def test_actionable_home_dead_route_uses_only_home_confirmed_backup(self):
        self.write_case(["CCTV-1"])
        payload = json.loads(self.health.read_text(encoding="utf-8"))
        payload["results"][0].update(status="GOOD", height=1080)
        self.health.write_text(json.dumps(payload), encoding="utf-8")
        self.write_home_evidence("CCTV-1")
        result = self.run_case()
        self.assertEqual(1, result["replacement_count"])
        self.assertEqual("home_confirmed_dead", result["decisions"][0]["reason"])
        self.assertTrue(result["home_probe"]["used"])

    def test_shadow_home_report_never_changes_a_hk_good_route(self):
        self.write_case(["CCTV-1"])
        payload = json.loads(self.health.read_text(encoding="utf-8"))
        payload["results"][0].update(status="GOOD", height=1080)
        self.health.write_text(json.dumps(payload), encoding="utf-8")
        self.write_home_evidence("CCTV-1", actionable=False)
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertEqual("shadow", result["home_probe"]["state"])

    def test_home_failure_waits_when_no_home_qualified_backup_exists(self):
        self.write_case(["CCTV-1"])
        payload = json.loads(self.health.read_text(encoding="utf-8"))
        payload["results"][0].update(status="GOOD", height=1080)
        self.health.write_text(json.dumps(payload), encoding="utf-8")
        self.write_home_evidence("CCTV-1", candidate_confirmed=False)
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertEqual(["CCTV-1"], result["unresolved_channels"])

    def test_home_report_is_ignored_after_playlist_changes(self):
        self.write_case(["CCTV-1"])
        payload = json.loads(self.health.read_text(encoding="utf-8"))
        payload["results"][0].update(status="GOOD", height=1080)
        self.health.write_text(json.dumps(payload), encoding="utf-8")
        self.write_home_evidence("CCTV-1", playlist_sha="f" * 64)
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertEqual("playlist_changed", result["home_probe"]["state"])

    def test_unverified_router_path_is_never_production_evidence(self):
        self.write_case(["CCTV-1"])
        payload = json.loads(self.health.read_text(encoding="utf-8"))
        payload["results"][0].update(status="GOOD", height=1080)
        self.health.write_text(json.dumps(payload), encoding="utf-8")
        self.write_home_evidence("CCTV-1", route_context="router-origin-direct-wan")
        result = self.run_case()
        self.assertEqual(0, result["replacement_count"])
        self.assertEqual("route_context_not_verified", result["home_probe"]["state"])

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
