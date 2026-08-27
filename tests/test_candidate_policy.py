from __future__ import annotations

import unittest

from scripts import build_playlist as bp
from scripts import candidate_policy


class CandidatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_measured_score = bp.measured_score
        self.original_fallback_rank = bp.publication_fallback_rank
        candidate_policy.apply(bp)

    def tearDown(self) -> None:
        bp.measured_score = self.original_measured_score
        bp.publication_fallback_rank = self.original_fallback_rank

    def channel(self, name: str, *, group: str = "广东", url: str = "http://example.test/live.m3u8") -> bp.Channel:
        return bp.Channel(
            name=name,
            extinf=f'#EXTINF:-1 group-title="{group}",{name}',
            url=url,
            group=group,
        )

    def probe(self, channel: bp.Channel, *, height: int, stream: float, speed: float, checks: int = 3) -> None:
        channel.probe = {
            "ok": True,
            "decoded_width": 1920 if height >= 1080 else 1280,
            "decoded_height": height,
            "width": 1920 if height >= 1080 else 1280,
            "height": height,
            "codec": "h264",
            "stream_mbps": stream,
            "segment_mbps": speed,
            "manifest_s": 0.2 if speed > 10 else 25.0,
            "checks_ok": checks,
        }

    def test_mainland_quality_beats_github_speed(self) -> None:
        clear = self.channel("广东测试A", url="http://example.test/a.m3u8")
        fast_low = self.channel("广东测试B", url="http://example.test/b.m3u8")
        self.probe(clear, height=1080, stream=5.0, speed=0.05)
        self.probe(fast_low, height=720, stream=2.0, speed=80.0)
        self.assertGreater(bp.measured_score(clear), bp.measured_score(fast_low))

    def test_mainland_same_resolution_prefers_intrinsic_bitrate(self) -> None:
        high_bitrate = self.channel("广东测试A", url="http://example.test/a.m3u8")
        fast_low_bitrate = self.channel("广东测试B", url="http://example.test/b.m3u8")
        self.probe(high_bitrate, height=1080, stream=5.0, speed=0.05)
        self.probe(fast_low_bitrate, height=1080, stream=2.1, speed=80.0)
        self.assertGreater(bp.measured_score(high_bitrate), bp.measured_score(fast_low_bitrate))

    def test_mainland_fallback_uses_decoded_height_not_label(self) -> None:
        fake = self.channel("广东测试 1080p")
        self.probe(fake, height=540, stream=3.0, speed=80.0)
        rank = bp.publication_fallback_rank(fake)
        self.assertFalse(rank[4])

    def test_overseas_keeps_original_network_policy(self) -> None:
        channel = self.channel("香港测试", group="香港")
        self.probe(channel, height=1080, stream=3.0, speed=20.0)
        expected = self.original_measured_score(channel)
        self.assertEqual(bp.measured_score(channel), expected)


if __name__ == "__main__":
    unittest.main()
