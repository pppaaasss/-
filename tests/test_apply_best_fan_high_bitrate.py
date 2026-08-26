import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apply_best_fan_high_bitrate.py"
SPEC = importlib.util.spec_from_file_location("apply_best_fan_high_bitrate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BestFanHighBitrateTests(unittest.TestCase):
    def test_prefers_raw_1080_route_and_keeps_cctv5plus_separate(self):
        source = "\n".join(
            (
                "#EXTM3U",
                '#EXTINF:-1 tvg-name="CCTV5[1080][S]",CCTV-5',
                "http://example.com/3m1080p/cctv5.m3u8",
                '#EXTINF:-1 tvg-name="CCTV5[1080][S]",CCTV-5',
                "http://1.2.3.4:9901/tsfile/live/0005_1.m3u8",
                '#EXTINF:-1 tvg-name="CCTV5[1080][S]",CCTV-5+',
                "http://1.2.3.4:9901/tsfile/live/0116_1.m3u8",
                "",
            )
        )
        routes = MODULE.preferred_routes(source)
        self.assertEqual(routes["cctv5"].url, "http://1.2.3.4:9901/tsfile/live/0005_1.m3u8")
        self.assertEqual(routes["cctv5plus"].url, "http://1.2.3.4:9901/tsfile/live/0116_1.m3u8")

    def test_replaces_only_url_and_does_not_use_720p_candidate(self):
        source = "\n".join(
            (
                "#EXTM3U",
                '#EXTINF:-1 tvg-name="湖南卫视[1080][S]",湖南卫视',
                "http://1.2.3.4:9901/tsfile/live/0128_1.m3u8",
                '#EXTINF:-1 tvg-name="CCTV16[720][S]",CCTV-16',
                "http://example.com/cctv16.m3u8",
                "",
            )
        )
        playlist = "\n".join(
            (
                '#EXTM3U x-tvg-url="https://example.com/e.xml"',
                "# channels=3",
                '#EXTINF:-1 tvg-id="湖南卫视" group-title="卫视台",湖南卫视',
                "http://old.example/hunan.m3u8",
                '#EXTINF:-1 tvg-id="CCTV16" group-title="卫视台",CCTV-16',
                "http://old.example/cctv16.m3u8",
                '#EXTINF:-1 group-title="日本",JOTX-DTV',
                "http://old.example/jotx.m3u8",
                "",
            )
        )
        routes = MODULE.preferred_routes(source)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.m3u"
            path.write_text(playlist, encoding="utf-8")
            result = MODULE.apply_overrides(path, routes)
            rendered = path.read_text(encoding="utf-8")
            second = MODULE.apply_overrides(path, routes)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["changed"], 1)
        self.assertIn("http://1.2.3.4:9901/tsfile/live/0128_1.m3u8", rendered)
        self.assertIn("http://old.example/cctv16.m3u8", rendered)
        self.assertIn("http://old.example/jotx.m3u8", rendered)
        self.assertIn('group-title="卫视台",湖南卫视', rendered)
        self.assertEqual(second["changed"], 0)

    def test_source_failure_is_non_destructive(self):
        with self.assertRaises(FileNotFoundError):
            MODULE.read_source("/definitely/missing/best-fan.m3u8")

    def test_cctv4_regional_services_are_not_replaced_by_mainland_route(self):
        source = "\n".join(
            (
                "#EXTM3U",
                '#EXTINF:-1 tvg-name="CCTV4[1080][S]",CCTV-4',
                "http://1.2.3.4:9901/tsfile/live/1003_1.m3u8",
                "",
            )
        )
        playlist = "\n".join(
            (
                "#EXTM3U",
                "# channels=2",
                '#EXTINF:-1 group-title="卫视台",CCTV-4',
                "http://old.example/cctv4.m3u8",
                '#EXTINF:-1 group-title="中文综合",CCTV4欧洲',
                "http://old.example/cctv4-europe.m3u8",
                "",
            )
        )
        routes = MODULE.preferred_routes(source)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.m3u"
            path.write_text(playlist, encoding="utf-8")
            result = MODULE.apply_overrides(path, routes)
            rendered = path.read_text(encoding="utf-8")
        self.assertEqual(result["matched"], 1)
        self.assertIn("http://1.2.3.4:9901/tsfile/live/1003_1.m3u8", rendered)
        self.assertIn("http://old.example/cctv4-europe.m3u8", rendered)


if __name__ == "__main__":
    unittest.main()
