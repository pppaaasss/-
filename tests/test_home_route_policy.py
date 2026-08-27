from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.home_route_policy import apply


class Channel:
    def __init__(self, url: str):
        self.url = url


class HomeRoutePolicyTests(unittest.TestCase):
    def make_feedback(self, root: Path, bad_url: str) -> Path:
        path = root / "feedback.json"
        path.write_text(
            json.dumps({"good": {}, "bad": {"cctv2": [{"url": bad_url}]}}),
            encoding="utf-8",
        )
        return path

    def make_bp(self):
        return SimpleNamespace(
            probe_channel=lambda channel: {"ok": True, "height": 1080},
            actual_height=lambda channel: 1080,
            is_stable=lambda channel: True,
            is_core_acceptable=lambda channel: True,
            measured_score=lambda channel: 123.0,
        )

    def test_bad_home_route_is_hard_rejected(self):
        bad_url = "http://example.test/cctv2.m3u8"
        with tempfile.TemporaryDirectory() as tmp:
            bp = self.make_bp()
            apply(bp, self.make_feedback(Path(tmp), bad_url))
            channel = Channel(bad_url)
            probe = bp.probe_channel(channel)
            self.assertFalse(probe["ok"])
            self.assertTrue(probe["home_feedback_rejected"])
            self.assertEqual(bp.actual_height(channel), 0)
            self.assertFalse(bp.is_stable(channel))
            self.assertFalse(bp.is_core_acceptable(channel))
            self.assertLess(bp.measured_score(channel), -999000)

    def test_unknown_route_keeps_normal_candidate_logic(self):
        bad_url = "http://example.test/bad.m3u8"
        good_url = "http://example.test/new.m3u8"
        with tempfile.TemporaryDirectory() as tmp:
            bp = self.make_bp()
            apply(bp, self.make_feedback(Path(tmp), bad_url))
            channel = Channel(good_url)
            self.assertTrue(bp.probe_channel(channel)["ok"])
            self.assertEqual(bp.actual_height(channel), 1080)
            self.assertTrue(bp.is_stable(channel))
            self.assertTrue(bp.is_core_acceptable(channel))
            self.assertEqual(bp.measured_score(channel), 123.0)


if __name__ == "__main__":
    unittest.main()
