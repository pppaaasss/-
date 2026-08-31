import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pin_viewer_channels as module


class PinViewerChannelsTests(unittest.TestCase):
    def test_keeps_builder_selected_hunan_url(self):
        hunan_url = "https://example.test/live/hunan.m3u8?token=fresh"
        playlist = "\n".join((
            "#EXTM3U",
            '#EXTINF:-1 group-title="卫视台",湖南卫视',
            hunan_url,
            '#EXTINF:-1 group-title="卫视台",CCTV-8',
            "https://example.test/live/cctv8k.m3u8",
            '#EXTINF:-1 group-title="卫视台",CCTV-4',
            "https://example.test/live/cctv4.m3u8",
            '#EXTINF:-1 group-title="卫视台",CCTV-1',
            "https://example.test/live/cctv1.m3u8",
            "",
        ))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tv.m3u"
            path.write_text(playlist, encoding="utf-8")
            changed = module.patch_playlist(path)
            rendered = path.read_text(encoding="utf-8")

        self.assertEqual(changed, ["cctv8", "cctv4", "cctv1"])
        self.assertIn(hunan_url, rendered)
        self.assertIn(module.TARGET_URLS["cctv8"], rendered)
        self.assertIn(module.TARGET_URLS["cctv4"], rendered)
        self.assertIn(module.TARGET_URLS["cctv1"], rendered)


if __name__ == "__main__":
    unittest.main()
