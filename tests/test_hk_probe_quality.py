import unittest
from unittest import mock

from scripts import hk_probe


class HongKongProbeQualityTests(unittest.TestCase):
    def test_newest_segment_keeps_extinf_duration(self):
        text = "\n".join((
            "#EXTM3U",
            "#EXTINF:4.0,",
            "part-a.ts",
            "#EXTINF:6.5,",
            "part-b.ts",
        ))
        url, duration = hk_probe.newest_segment(text, "https://example.test/live/index.m3u8")
        self.assertEqual("https://example.test/live/part-b.ts", url)
        self.assertEqual(6.5, duration)

    @mock.patch.object(
        hk_probe,
        "hls_segment_probe",
        return_value=(True, 20.0, 8.5, 0.5, ""),
    )
    @mock.patch.object(
        hk_probe,
        "ffprobe_meta",
        return_value={
            "width": 1920,
            "height": 1080,
            "codec": "h264",
            "field_order": "progressive",
            "fps": 25.0,
            "bitrate_mbps": 0.0,
        },
    )
    def test_segment_duration_supplies_missing_intrinsic_bitrate(self, _meta, _segment):
        result = hk_probe.probe_one(("CCTV-8", "https://example.test/live.m3u8", 1080))
        self.assertEqual("GOOD", result.status)
        self.assertEqual(8.5, result.stream_mbps)
        self.assertEqual(8.5, result.bitrate_mbps)


if __name__ == "__main__":
    unittest.main()
