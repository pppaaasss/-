from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import build_playlist as bp


class DecodedQualityPolicyTests(unittest.TestCase):
    def channel(self, name: str, group: str = "大陆", url: str = "http://example.test/live.m3u8") -> bp.Channel:
        return bp.Channel(
            name=name,
            extinf=f'#EXTINF:-1 group-title="{group}",{name}',
            url=url,
            group=group,
        )

    def healthy_probe(self, channel: bp.Channel, *, height: int, codec: str = "h264", stream: float = 3.0,
                      speed: float = 0.1, latency: float = 30.0, checks: int = 3) -> None:
        channel.probe = {
            "ok": True,
            "decoded_width": 1920 if height >= 1080 else 1280,
            "decoded_height": height,
            "width": 1920 if height >= 1080 else 1280,
            "height": height,
            "codec": codec,
            "stream_mbps": stream,
            "segment_mbps": speed,
            "manifest_s": latency,
            "checks_ok": checks,
            "is_master_playlist": False,
            "publish_url": channel.url,
        }

    def test_unknown_resolution_cannot_be_core_primary(self) -> None:
        channel = self.channel("CCTV-5")
        self.healthy_probe(channel, height=0)
        self.assertFalse(bp.is_core_acceptable(channel))

    def test_core_720_cannot_be_primary(self) -> None:
        channel = self.channel("CCTV-8")
        self.healthy_probe(channel, height=720)
        self.assertFalse(bp.is_core_acceptable(channel))
        self.assertFalse(bp.is_family_core_usable(channel))

    def test_cctv4k_requires_2160(self) -> None:
        channel = self.channel("CCTV-4K")
        self.healthy_probe(channel, height=1080, codec="hevc", stream=8.0)
        self.assertFalse(bp.is_core_acceptable(channel))
        self.healthy_probe(channel, height=2160, codec="hevc", stream=8.0)
        self.assertTrue(bp.is_core_acceptable(channel))

    def test_fake_1080_name_is_overridden_by_decoded_height(self) -> None:
        channel = self.channel("北京地方台 1080p", group="北京")
        self.healthy_probe(channel, height=540)
        self.assertFalse(bp.is_stable(channel))
        self.assertFalse(bp.is_easy_ready(channel))

    def test_480_540_576_cannot_enter_family(self) -> None:
        for height in (480, 540, 576):
            with self.subTest(height=height):
                channel = self.channel(f"地方频道 {height}p", group="广东", url=f"http://example.test/{height}.m3u8")
                self.healthy_probe(channel, height=height)
                self.assertFalse(bp.is_easy_ready(channel))

    def test_mainland_speed_is_advisory_not_rejection(self) -> None:
        channel = self.channel("CCTV-5")
        self.healthy_probe(channel, height=1080, stream=3.5, speed=0.05, latency=45.0, checks=2)
        self.assertTrue(bp.is_core_acceptable(channel))
        self.assertTrue(bp.is_stable(channel))

    def test_low_bitrate_h264_fake_1080_is_rejected(self) -> None:
        channel = self.channel("CCTV-5")
        self.healthy_probe(channel, height=1080, stream=0.7)
        self.assertFalse(bp.is_core_acceptable(channel))

    def test_adaptive_master_publishes_verified_media_variant(self) -> None:
        channel = self.channel("测试频道", group="香港", url="https://example.test/master.m3u8")
        self.healthy_probe(channel, height=1080, speed=10.0, latency=0.5)
        channel.probe.update({
            "is_master_playlist": True,
            "tested_variant_url": "https://example.test/1080/index.m3u8",
            "publish_url": "https://example.test/1080/index.m3u8",
        })
        self.assertEqual(bp.publication_url(channel), "https://example.test/1080/index.m3u8")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.m3u"
            bp.write_playlist(path, [channel], "test")
            text = path.read_text(encoding="utf-8")
            self.assertIn("https://example.test/1080/index.m3u8", text)
            self.assertNotIn("https://example.test/master.m3u8\n", text)

    def test_cctv8_to_cctv8k_url_is_rejected(self) -> None:
        channel = self.channel("CCTV-8", url="https://example.test/live/cctv8k/index.m3u8")
        self.assertTrue(bp.cctv_url_conflicts_with_label(channel))

    def test_select_main_does_not_reintroduce_low_core(self) -> None:
        low = self.channel("CCTV-8")
        self.healthy_probe(low, height=720)
        chosen = bp.select_main([], [low], target=10)
        self.assertEqual(chosen, [])

    def test_select_all_labels_low_resolution(self) -> None:
        low = self.channel("地方频道", group="广东")
        self.healthy_probe(low, height=540)
        chosen = bp.select_all([low], [])
        self.assertEqual(len(chosen), 1)
        self.assertIn("[540p]", bp.canonical_display_name(chosen[0]))


if __name__ == "__main__":
    unittest.main()
