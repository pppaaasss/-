import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "repair_cctv5.py"
SPEC = importlib.util.spec_from_file_location("repair_cctv5", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)


class RepairCctv5PairTests(unittest.TestCase):
    def test_station_keys_are_independent(self):
        self.assertEqual(repair.station_key("#EXTINF:-1,CCTV-5"), "cctv5")
        self.assertEqual(repair.station_key("#EXTINF:-1,CCTV-5+"), "cctv5plus")
        self.assertEqual(repair.station_key("#EXTINF:-1,CCTV-5 备用1 1080p"), "cctv5")

    def test_selection_uses_distinct_healthy_hosts(self):
        cctv5_url = repair.CANDIDATES["cctv5"][0]
        plus_url = repair.CANDIDATES["cctv5plus"][0]
        results = [
            repair.Probe("cctv5", url, url == cctv5_url, 8.0)
            for url in repair.CANDIDATES["cctv5"]
        ] + [
            repair.Probe("cctv5plus", url, url == plus_url, 7.0)
            for url in repair.CANDIDATES["cctv5plus"]
        ]
        selected = repair.choose_routes(results, {})
        self.assertEqual(selected["cctv5"].url, cctv5_url)
        self.assertEqual(selected["cctv5plus"].url, plus_url)
        self.assertNotEqual(
            repair.urllib.parse.urlsplit(selected["cctv5"].url).hostname,
            repair.urllib.parse.urlsplit(selected["cctv5plus"].url).hostname,
        )

    def test_selection_prefers_high_bitrate_with_download_headroom(self):
        low_quality = repair.CANDIDATES["cctv5"][0]
        high_quality = repair.CANDIDATES["cctv5"][1]
        results = []
        for url in repair.CANDIDATES["cctv5"]:
            if url == low_quality:
                results.append(repair.Probe("cctv5", url, True, 30.0, 2.0, 1080))
            elif url == high_quality:
                results.append(repair.Probe("cctv5", url, True, 15.0, 6.0, 1080))
            else:
                results.append(repair.Probe("cctv5", url, False))
        for index, url in enumerate(repair.CANDIDATES["cctv5plus"]):
            results.append(
                repair.Probe("cctv5plus", url, index < 2, 12.0, 4.0, 1080)
            )
        selected = repair.choose_routes(results, {})
        self.assertEqual(selected["cctv5"].url, high_quality)
        self.assertNotEqual(
            selected["cctv5"].probe.host,
            selected["cctv5plus"].probe.host,
        )

    def test_pair_optimizer_does_not_waste_best_plus_route(self):
        five_fast_host = repair.CANDIDATES["cctv5"][1]
        five_alternate = repair.CANDIDATES["cctv5"][2]
        plus_fast_host = repair.CANDIDATES["cctv5plus"][0]
        plus_alternate = repair.CANDIDATES["cctv5plus"][1]
        results = []
        for url in repair.CANDIDATES["cctv5"]:
            if url == five_fast_host:
                results.append(repair.Probe("cctv5", url, True, 56.0, 2.26))
            elif url == five_alternate:
                results.append(repair.Probe("cctv5", url, True, 15.0, 2.26))
            else:
                results.append(repair.Probe("cctv5", url, False))
        for url in repair.CANDIDATES["cctv5plus"]:
            if url == plus_fast_host:
                results.append(repair.Probe("cctv5plus", url, True, 99.0, 3.38))
            elif url == plus_alternate:
                results.append(repair.Probe("cctv5plus", url, True, 25.0, 1.62))
            else:
                results.append(repair.Probe("cctv5plus", url, False))
        selected = repair.choose_routes(results, {})
        self.assertEqual(selected["cctv5"].url, five_alternate)
        self.assertEqual(selected["cctv5plus"].url, plus_fast_host)

    def test_segment_duration_supports_real_stream_bitrate(self):
        text = """#EXTM3U
#EXTINF:5.5,
segment-1.ts
"""
        url, duration = repair.first_media_segment(text, "https://cdn.example/live/index.m3u8")
        self.assertEqual(url, "https://cdn.example/live/segment-1.ts")
        self.assertEqual(duration, 5.5)

    def test_probe_reads_a_full_sized_media_segment_for_bitrate(self):
        calls = []
        manifest = b"#EXTM3U\n#EXTINF:4.0,\nsegment.ts\n"
        segment = b"x" * 1_000_000
        original_fetch = repair.fetch

        def fake_fetch(url, limit=repair.READ_LIMIT):
            calls.append((url, limit))
            if url.endswith("segment.ts"):
                return segment, url, 0.5
            return manifest, url, 0.01

        repair.fetch = fake_fetch
        try:
            download, stream, height = repair.probe_once("https://cdn.example/live.m3u8")
        finally:
            repair.fetch = original_fetch
        self.assertEqual(calls[-1][1], repair.SEGMENT_READ_LIMIT)
        self.assertAlmostEqual(download, 16.0)
        self.assertAlmostEqual(stream, 2.0)
        self.assertEqual(height, 0)

    def test_rewrite_removes_dead_pair_and_duplicate_backup(self):
        original = """#EXTM3U
# channels=4
#EXTINF:-1 tvg-name=\"CCTV5\",CCTV-5
http://219.140.56.34:3333/old5.m3u8
#EXTINF:-1 tvg-name=\"CCTV5\",CCTV-5 备用1 1080p
http://example.invalid/backup.m3u8
#EXTINF:-1 tvg-name=\"CCTV5+\",CCTV-5+
http://219.140.56.34:3333/old5p.m3u8
#EXTINF:-1,湖南卫视
https://example.com/hunan.m3u8
"""
        selections = {
            "cctv5": repair.Selection("http://one.example/cctv5.m3u8", "test"),
            "cctv5plus": repair.Selection("http://two.example/cctv5p.m3u8", "test"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.m3u"
            path.write_text(original, encoding="utf-8")
            self.assertTrue(repair.rewrite_playlist(path, selections))
            updated = path.read_text(encoding="utf-8")
        self.assertNotIn("219.140.56.34", updated)
        self.assertNotIn("备用", updated)
        self.assertEqual(updated.count(",CCTV-5\n"), 1)
        self.assertEqual(updated.count(",CCTV-5+\n"), 1)
        self.assertIn("# channels=3", updated)


if __name__ == "__main__":
    unittest.main()
