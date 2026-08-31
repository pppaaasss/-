import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.publish_hk_health import (
    build_put_payload,
    commit_message,
    load_report,
    publish,
    validate_destination,
)


def valid_report():
    return {
        "generated_utc": "2026-08-28T13:30:00Z",
        "production_modified": False,
        "policy": {"auto_replace_formal_routes": False},
        "summary": {"channels": 1, "good": 1, "degraded": 0, "unknown": 0},
        "results": [{"name": "CCTV-1", "status": "GOOD"}],
    }


def valid_failover_report():
    return {
        "generated_utc": "2026-08-29T05:34:50Z",
        "applied": True,
        "selected_updates": [],
        "changed_files": [],
        "decisions": [],
        "policy": {"dead_only": True},
    }


def valid_home_report():
    url = "https://one.test/live.m3u8"
    sample = {
        "url": "https://one.test/one.ts",
        "downloaded_bytes": 2097152,
        "total_bytes": 3145728,
        "duration_s": 4.0,
        "elapsed_s": 1.0,
        "download_mbps": 16.0,
        "stream_mbps": 6.0,
        "complete": False,
    }
    row = {
        "name": "CCTV-1", "url": url, "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
        "status": "GOOD", "observed_status": "GOOD", "sample_count": 2,
        "segment_samples": [sample, sample], "startup_s": 1.0,
        "min_download_mbps": 16.0, "avg_download_mbps": 16.0, "stream_mbps": 6.0,
        "headroom_ratio": 2.667, "width": 1920, "height": 1080, "codec": "h264",
        "fps": 50.0, "bitrate_mbps": 6.0, "min_height": 1080, "error": "",
        "consecutive_failures": 0, "failure_age_hours": 0.0, "home_dead_confirmed": False,
        "consecutive_degraded": 0, "degraded_age_hours": 0.0, "home_degraded_confirmed": False,
    }
    return {
        "schema": "iptv-home-probe/v1", "probe_id": "home-ac86u-123",
        "generated_utc": "2026-08-31T18:00:00Z", "run_status": "COMPLETED", "mode": "deep",
        "actionable": False, "production_modified": False,
        "playlist": {"url": "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u", "sha256": "a" * 64, "channel_count": 1},
        "candidate_playlist": None,
        "policy": {"auto_replace_formal_routes": False, "mass_failure_circuit_breaker": True, "samples_per_route": 2, "sample_bytes": 2097152},
        "resources": {"load1": 0.2, "mem_available_kib": 100000, "runtime_s": 10.0},
        "summary": {"channels": 1, "good": 1, "degraded": 0, "unknown": 0, "dead": 0, "candidate_channels": 0, "candidate_confirmed": 0, "circuit_breaker_open": False},
        "results": [row], "candidate_results": [],
        "transport": {"via": "ssh-forced-command", "receiver_validated": True, "received_utc": "2026-08-31T18:01:00Z", "report_sha256": "b" * 64},
    }


class HealthPublisherSafetyTests(unittest.TestCase):
    def write_report(self, payload):
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        with handle:
            json.dump(payload, handle)
        return Path(handle.name)

    def test_accepts_read_only_probe_report(self):
        path = self.write_report(valid_report())
        try:
            report, raw = load_report(path)
            self.assertFalse(report["production_modified"])
            self.assertIn(b"CCTV-1", raw)
        finally:
            path.unlink(missing_ok=True)

    def test_rejects_report_that_may_modify_production(self):
        payload = valid_report()
        payload["production_modified"] = True
        path = self.write_report(payload)
        try:
            with self.assertRaises(RuntimeError):
                load_report(path)
        finally:
            path.unlink(missing_ok=True)

    def test_refuses_master_or_other_destination(self):
        with self.assertRaises(RuntimeError):
            validate_destination("pppaaasss/-", "master", "health/latest.json")
        with self.assertRaises(RuntimeError):
            validate_destination("pppaaasss/-", "health-monitor", "tv.m3u")

    def test_accepts_scoped_dead_only_failover_report(self):
        destination = "health/dead-only-failover.json"
        validate_destination("pppaaasss/-", "health-monitor", destination)
        path = self.write_report(valid_failover_report())
        try:
            report, raw = load_report(path, destination)
            self.assertTrue(report["policy"]["dead_only"])
            self.assertIn(b"selected_updates", raw)
            self.assertIn("SELECTED=0", commit_message(report, destination))
        finally:
            path.unlink(missing_ok=True)

    def test_failover_report_rejects_unexpected_file_scope(self):
        payload = valid_failover_report()
        payload["changed_files"] = ["README.md"]
        path = self.write_report(payload)
        try:
            with self.assertRaises(RuntimeError):
                load_report(path, "health/dead-only-failover.json")
        finally:
            path.unlink(missing_ok=True)

    def test_failover_report_accepts_more_than_three_unique_channel_updates(self):
        payload = valid_failover_report()
        payload["selected_updates"] = [
            {
                "channel": f"CCTV-{number}",
                "old_url": f"http://old.test/{number}.m3u8",
                "new_url": f"http://new.test/{number}.m3u8",
                "matching_files": ["tv.m3u"],
            }
            for number in range(1, 6)
        ]
        path = self.write_report(payload)
        try:
            report, _ = load_report(path, "health/dead-only-failover.json")
            self.assertEqual(5, len(report["selected_updates"]))
        finally:
            path.unlink(missing_ok=True)

    def test_accepts_only_receiver_validated_home_report(self):
        destination = "health/home-latest.json"
        payload = valid_home_report()
        path = self.write_report(payload)
        try:
            validate_destination("pppaaasss/-", "health-monitor", destination)
            report, _ = load_report(path, destination)
            self.assertIn("ACTIONABLE=0", commit_message(report, destination))
        finally:
            path.unlink(missing_ok=True)
        payload.pop("transport")
        path = self.write_report(payload)
        try:
            with self.assertRaisesRegex(RuntimeError, "transport"):
                load_report(path, destination)
        finally:
            path.unlink(missing_ok=True)

    def test_update_payload_is_bound_to_health_branch(self):
        raw = b'{"production_modified":false}'
        payload = build_put_payload(raw, "health-monitor", "old-blob", "health")
        self.assertEqual("health-monitor", payload["branch"])
        self.assertEqual("old-blob", payload["sha"])
        self.assertEqual(raw, base64.b64decode(payload["content"]))

    def test_missing_token_is_a_hard_failure_not_a_silent_success(self):
        report = self.write_report(valid_report())
        missing_token = report.with_name("missing-token")
        try:
            with self.assertRaisesRegex(RuntimeError, "no token"):
                publish(report, "pppaaasss/-", "health-monitor", "health/latest.json", missing_token)
        finally:
            report.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
