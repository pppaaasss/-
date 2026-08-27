import tempfile
import unittest
from pathlib import Path

from scripts import maintain_published_channels as maintenance


class LocalMaintenanceTests(unittest.TestCase):
    def test_name_key_normalizes_quality_suffixes(self):
        self.assertEqual(maintenance.name_key("苏州新闻综合 1080P"), "苏州新闻综合")
        self.assertEqual(maintenance.name_key("南京新闻[HD]"), "南京新闻")

    def test_rewrite_replaces_dead_local_and_keeps_other_groups(self):
        text = """#EXTM3U
#EXTINF:-1 group-title=\"江苏\",苏州新闻
http://dead.example/suzhou.m3u8
#EXTINF:-1 group-title=\"中文付费\",凤凰中文
http://pay.example/phoenix.m3u8
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tv.m3u"
            path.write_text(text, encoding="utf-8")
            replaced, removed = maintenance.rewrite_playlist(
                path,
                text,
                {"http://dead.example/suzhou.m3u8"},
                {maintenance.name_key("苏州新闻"): "http://live.example/suzhou.m3u8"},
            )
            rendered = path.read_text(encoding="utf-8")
        self.assertEqual((replaced, removed), (1, 0))
        self.assertIn("http://live.example/suzhou.m3u8", rendered)
        self.assertIn("http://pay.example/phoenix.m3u8", rendered)

    def test_rewrite_removes_confirmed_dead_local_without_alternate(self):
        text = """#EXTM3U
#EXTINF:-1 group-title=\"广东\",某地方台
http://dead.example/local.m3u8
#EXTINF:-1 group-title=\"卫视台\",CCTV-1
http://keep.example/cctv1.m3u8
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tv.m3u"
            path.write_text(text, encoding="utf-8")
            replaced, removed = maintenance.rewrite_playlist(
                path,
                text,
                {"http://dead.example/local.m3u8"},
                {maintenance.name_key("某地方台"): None},
            )
            rendered = path.read_text(encoding="utf-8")
        self.assertEqual((replaced, removed), (0, 1))
        self.assertNotIn("某地方台", rendered)
        self.assertIn("CCTV-1", rendered)


if __name__ == "__main__":
    unittest.main()
