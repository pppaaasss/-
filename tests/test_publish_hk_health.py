import base64
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
