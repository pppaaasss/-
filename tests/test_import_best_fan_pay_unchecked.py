import tempfile
import unittest
from pathlib import Path

from scripts import import_best_fan_pay_unchecked as pay


class PayMirrorTests(unittest.TestCase):
    def test_parse_index_forces_pay_group(self):
        text = """#EXTM3U
#EXTINF:-1 group-title=\"其他频道\",凤凰中文
http://example.com/phoenix.m3u8
"""
        entries = pay.parse_index(text)
        self.assertEqual(len(entries), 1)
        self.assertIn('group-title="中文付费"', entries[0][1])
        self.assertEqual(entries[0][2], "http://example.com/phoenix.m3u8")

    def test_patch_replaces_old_visible_name_and_preserves_other_channel(self):
        upstream = [
            (
                pay.name_key("凤凰中文"),
                '#EXTINF:-1 group-title="中文付费",凤凰中文',
                "http://new.example/phoenix.m3u8",
            )
        ]
        original = """#EXTM3U
#EXTINF:-1 group-title=\"香港\",凤凰中文
http://old.example/phoenix.m3u8
#EXTINF:-1 group-title=\"卫视台\",CCTV-1
http://example.com/cctv1.m3u8
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tv.m3u"
            path.write_text(original, encoding="utf-8")
            count = pay.patch_playlist(path, upstream)
            rendered = path.read_text(encoding="utf-8")
        self.assertEqual(count, 1)
        self.assertIn(pay.MARKER, rendered)
        self.assertIn("http://new.example/phoenix.m3u8", rendered)
        self.assertNotIn("http://old.example/phoenix.m3u8", rendered)
        self.assertIn("http://example.com/cctv1.m3u8", rendered)


if __name__ == "__main__":
    unittest.main()
