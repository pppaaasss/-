import base64
import json
import tempfile
import unittest
from pathlib import Path

from scripts.publish_hk_health import build_put_payload, load_report, validate_destination


def valid_report():
    return {
        "generated_utc": "2026-08-28T13:30:00Z",
        "production_modified": False,
        "policy": {"auto_replace_formal_routes": False},
        "summary": {"channels": 1, "good": 1, "degraded": 0, "unknown": 0},
        "results": [{"name": "CCTV-1", "status": "GOOD"}],
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

    def test_update_payload_is_bound_to_health_branch(self):
        raw = b'{"production_modified":false}'
        payload = build_put_payload(raw, "health-monitor", "old-blob", "health")
        self.assertEqual("health-monitor", payload["branch"])
        self.assertEqual("old-blob", payload["sha"])
        self.assertEqual(raw, base64.b64decode(payload["content"]))


if __name__ == "__main__":
    unittest.main()
