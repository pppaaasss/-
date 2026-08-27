from __future__ import annotations

import time
import unittest

from scripts import build_playlist as bp
from scripts import candidate_policy
from scripts import gap_candidate_patch


class CandidatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_measured_score = bp.measured_score
        self.original_fallback_rank = bp.publication_fallback_rank
        self.original_add_cctv5_backups = bp.add_cctv5_backups
        self.original_merge_probe_results = bp.merge_probe_results
        self.original_has_short_token = bp.has_short_token
        candidate_policy.apply(bp)
        gap_candidate_patch.apply(bp)

    def tearDown(self) -> None:
        bp.measured_score = self.original_measured_score
        bp.publication_fallback_rank = self.original_fallback_rank
        bp.add_cctv5_backups = self.original_add_cctv5_backups
        bp.merge_probe_results = self.original_merge_probe_results
        bp.has_short_token = self.original_has_short_token

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
            "is_master_playlist": False,
            "publish_url": channel.url,
        }

    def test_candidate_source_pool_includes_autoiptv_hotel_once(self) -> None:
        self.assertIn(candidate_policy.HOTEL_SOURCE, bp.SOURCES)
        before = bp.SOURCES.count(candidate_policy.HOTEL_SOURCE)
        candidate_policy.apply(bp)
        self.assertEqual(bp.SOURCES.count(candidate_policy.HOTEL_SOURCE), before)

    def test_tianjin_primary_routes_are_candidate_only_and_idempotent(self) -> None:
        primary_cctv1 = (
            "CCTV-1",
            "大陆",
            "http://111.32.21.78/PLTV/88888888/224/3221226366/1.m3u8",
        )
        low_cctv1 = (
            "CCTV-1",
            "大陆",
            "http://111.32.21.78/PLTV/88888888/224/3221226550/1.m3u8",
        )
        self.assertIn(primary_cctv1, bp.EXTRAS)
        self.assertNotIn(low_cctv1, bp.EXTRAS)
        before = bp.EXTRAS.count(primary_cctv1)
        candidate_policy.apply(bp)
        self.assertEqual(bp.EXTRAS.count(primary_cctv1), before)

    def test_targeted_gap_routes_are_candidate_only_and_idempotent(self) -> None:
        cctv4k = (
            "CCTV-4K",
            "大陆",
            "http://222.85.69.6/PLTV/88888888/224/3221227244/index.m3u8",
        )
        sansha = (
            "三沙卫视",
            "大陆",
            "http://www.hiiptv.cn:6060/000000001000/4600001000000000117/1.m3u8?Contentid=4600001000000000117&stbId=005103FF00010060000100E400F75DA4",
        )
        self.assertIn(cctv4k, bp.EXTRAS)
        self.assertIn(sansha, bp.EXTRAS)
        before = bp.EXTRAS.count(cctv4k)
        gap_candidate_patch.apply(bp)
        self.assertEqual(bp.EXTRAS.count(cctv4k), before)

    def test_auth_key_epoch_expiring_within_day_is_rejected(self) -> None:
        expiry = int(time.time()) + 3600
        url = f"https://example.test/live.m3u8?auth_key={expiry}-1-deadbeef"
        self.assertTrue(bp.has_short_token(url))

    def test_far_future_or_opaque_auth_key_is_not_false_positive(self) -> None:
        expiry = int(time.time()) + 3 * 24 * 3600
        self.assertFalse(bp.has_short_token(f"https://example.test/live.m3u8?auth_key={expiry}-1-deadbeef"))
        self.assertFalse(bp.has_short_token("https://example.test/live.m3u8?auth_key=opaque-token"))

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

    def test_cctv5_backup_rejects_fake_decoded_720(self) -> None:
        primary = self.channel("CCTV-5", group="大陆", url="http://primary.test/cctv5.m3u8")
        fake = self.channel("CCTV-5", group="大陆", url="http://backup.test/cctv5.m3u8")
        self.probe(primary, height=1080, stream=3.5, speed=0.05)
        self.probe(fake, height=720, stream=3.5, speed=80.0)
        fake.probe["height"] = 1080
        output = bp.add_cctv5_backups([primary], [fake], count=1)
        self.assertFalse(any(str(channel.display_override or "").startswith("CCTV-5 备用") for channel in output))

    def test_recheck_downgrade_updates_decoded_height(self) -> None:
        first = {
            "ok": True,
            "decoded_width": 1920,
            "decoded_height": 1080,
            "width": 1920,
            "height": 1080,
            "segment_mbps": 10.0,
            "manifest_s": 0.5,
            "stream_mbps": 4.0,
        }
        later = {
            "ok": True,
            "decoded_width": 1280,
            "decoded_height": 720,
            "width": 1280,
            "height": 720,
            "segment_mbps": 9.0,
            "manifest_s": 0.7,
            "stream_mbps": 3.0,
        }
        merged = bp.merge_probe_results(first, later, 2)
        self.assertEqual(merged["decoded_height"], 720)
        self.assertEqual(merged["height"], 720)
        self.assertEqual(merged["decoded_width"], 1280)

    def test_overseas_keeps_original_network_policy(self) -> None:
        channel = self.channel("香港测试", group="香港")
        self.probe(channel, height=1080, stream=3.0, speed=20.0)
        expected = self.original_measured_score(channel)
        self.assertEqual(bp.measured_score(channel), expected)


if __name__ == "__main__":
    unittest.main()
