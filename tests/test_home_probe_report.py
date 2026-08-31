import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.home_probe_report import load_home_report_bytes, validate_home_report
from scripts.receive_home_probe import receive


UTC = timezone.utc


def sample_result(name="CCTV-1", url="https://one.test/live.m3u8"):
    return {
        "name": name,
        "url": url,
        "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
        "status": "GOOD",
        "observed_status": "GOOD",
        "sample_count": 2,
        "segment_samples": [
            {
                "url": f"https://one.test/{index}.ts",
                "downloaded_bytes": 2 * 1024 * 1024,
                "total_bytes": 3 * 1024 * 1024,
                "duration_s": 4.0,
                "elapsed_s": 1.0,
                "download_mbps": 16.777,
                "stream_mbps": 6.291,
                "complete": False,
            }
            for index in (1, 2)
        ],
        "startup_s": 1.2,
        "min_download_mbps": 16.777,
        "avg_download_mbps": 16.777,
        "stream_mbps": 6.291,
        "headroom_ratio": 2.667,
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 50.0,
        "bitrate_mbps": 6.291,
        "min_height": 1080,
        "deep_checked": True,
        "error": "",
        "consecutive_failures": 0,
        "failure_age_hours": 0.0,
        "home_dead_confirmed": False,
        "consecutive_degraded": 0,
        "degraded_age_hours": 0.0,
        "home_degraded_confirmed": False,
    }


def valid_report(generated="2026-08-31T18:00:00Z"):
    row = sample_result()
    return {
        "schema": "iptv-home-probe/v1",
        "probe_id": "home-ac86u-123",
        "generated_utc": generated,
        "run_status": "COMPLETED",
        "mode": "deep",
        "actionable": False,
        "production_modified": False,
        "probe_region": "home",
        "route_context": "router-origin-direct-wan",
        "playlist": {
            "url": "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u",
            "sha256": "a" * 64,
            "channel_count": 1,
        },
        "candidate_playlist": None,
        "policy": {
            "auto_replace_formal_routes": False,
            "mass_failure_circuit_breaker": True,
            "samples_per_route": 2,
            "sample_bytes": 2 * 1024 * 1024,
        },
        "resources": {"load1": 0.2, "mem_available_kib": 180000, "runtime_s": 30.0},
        "summary": {
            "channels": 1,
            "good": 1,
            "degraded": 0,
            "unknown": 0,
            "dead": 0,
            "candidate_channels": 0,
            "candidate_confirmed": 0,
            "circuit_breaker_open": False,
        },
        "results": [row],
        "candidate_results": [],
    }


class HomeProbeReportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 18, 5, tzinfo=UTC).timestamp()

    def test_valid_report_round_trip(self):
        payload = valid_report()
        raw = json.dumps(payload).encode()
        parsed = load_home_report_bytes(raw, expected_probe_id="home-ac86u-123", now_epoch=self.now)
        self.assertEqual("CCTV-1", parsed["results"][0]["name"])

    def test_url_hash_and_summary_are_bound_to_results(self):
        payload = valid_report()
        payload["results"][0]["url"] = "https://changed.test/live.m3u8"
        with self.assertRaisesRegex(RuntimeError, "url_sha256"):
            validate_home_report(payload)
        payload = valid_report()
        payload["summary"]["good"] = 0
        with self.assertRaisesRegex(RuntimeError, "summary.good"):
            validate_home_report(payload)

    def test_duplicate_channel_and_unconfirmed_dead_are_rejected(self):
        payload = valid_report()
        payload["results"].append(copy.deepcopy(payload["results"][0]))
        payload["playlist"]["channel_count"] = 2
        payload["summary"]["channels"] = 2
        payload["summary"]["good"] = 2
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            validate_home_report(payload)
        payload = valid_report()
        payload["results"][0]["status"] = "DEAD"
        payload["summary"].update(good=0, dead=1)
        with self.assertRaisesRegex(RuntimeError, "without confirmation"):
            validate_home_report(payload)

    def test_receiver_atomically_accepts_duplicate_but_rejects_replay(self):
        raw = json.dumps(valid_report()).encode()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            status, latest = receive(
                raw,
                expected_probe_id="home-ac86u-123",
                output_dir=output,
                now_epoch=self.now,
            )
            self.assertEqual("accepted", status)
            received = json.loads(latest.read_text(encoding="utf-8"))
            self.assertTrue(received["transport"]["receiver_validated"])
            status, _ = receive(
                raw,
                expected_probe_id="home-ac86u-123",
                output_dir=output,
                now_epoch=self.now,
            )
            self.assertEqual("duplicate", status)

            older = json.dumps(valid_report("2026-08-31T17:59:59Z")).encode()
            with self.assertRaisesRegex(RuntimeError, "older"):
                receive(
                    older,
                    expected_probe_id="home-ac86u-123",
                    output_dir=output,
                    now_epoch=self.now,
                )

    def test_receiver_rejects_wrong_probe_and_oversize(self):
        raw = json.dumps(valid_report()).encode()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                receive(
                    raw,
                    expected_probe_id="some-other-probe",
                    output_dir=Path(temporary),
                    now_epoch=self.now,
                )
        with self.assertRaisesRegex(RuntimeError, "too large"):
            load_home_report_bytes(b" " * 900_001)


if __name__ == "__main__":
    unittest.main()
