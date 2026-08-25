import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_playlist.py"
SPEC = importlib.util.spec_from_file_location("build_playlist", SCRIPT)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


class BuildPlaylistTests(unittest.TestCase):
    def test_default_targets_are_expanded(self):
        self.assertEqual(builder.TARGET_STABLE, 800)
        self.assertEqual(builder.MIN_STABLE, 760)
        self.assertEqual(builder.TARGET_ALL, 1000)

    def test_parser_preserves_source_and_normalizes_group(self):
        playlist = """#EXTM3U
#EXTINF:-1 group-title="香港",TVB翡翠台
https://example.com/live.m3u8
"""
        channels = builder.parse_m3u(playlist, "中文综合", True, source="fixture")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].group, "香港")
        self.assertEqual(channels[0].source, "fixture")

    def test_notice_entries_are_rejected_before_probe(self):
        playlist = """#EXTM3U
#EXTINF:-1 tvg-id="免费订阅",维护内容：请勿贩卖
https://example.com/notice.m3u8
"""
        self.assertEqual(builder.parse_m3u(playlist, "中文综合", False), [])

    def test_user_confirmed_test_card_is_rejected(self):
        channel = builder.Channel(
            "CCTV-13 新闻",
            '#EXTINF:-1 group-title="大陆",CCTV-13 新闻',
            "https://event.pull.hebtv.com/jishi/cp1.m3u8",
            "大陆",
        )
        self.assertFalse(builder.is_station_like(channel))

    def test_adjacent_hebtv_test_card_is_rejected(self):
        channel = builder.Channel(
            "CCTV-14 少儿",
            '#EXTINF:-1 group-title="大陆",CCTV-14 少儿',
            "https://event.pull.hebtv.com/jishi/cp2.m3u8",
            "大陆",
        )
        self.assertFalse(builder.is_station_like(channel))

    def test_cctv_label_and_url_conflicts_are_rejected(self):
        wrong_station = builder.Channel(
            "CCTV-4 中文国际",
            '#EXTINF:-1 group-title="大陆",CCTV-4 中文国际',
            "https://global.cgtn.example/master/cgtn-america.m3u8",
            "大陆",
        )
        wrong_number = builder.Channel(
            "CCTV-13 新闻",
            '#EXTINF:-1 group-title="大陆",CCTV-13 新闻',
            "https://example.com/live/cctv14hd.m3u8",
            "大陆",
        )
        correct = builder.Channel(
            "CCTV-13 新闻",
            '#EXTINF:-1 group-title="大陆",CCTV-13 新闻',
            "https://example.com/live/cctv13hd.m3u8",
            "大陆",
        )
        self.assertFalse(builder.is_station_like(wrong_station))
        self.assertFalse(builder.is_station_like(wrong_number))
        self.assertTrue(builder.is_station_like(correct))

    def test_user_rejected_4k_and_music_routes_are_rejected(self):
        cctv4k = builder.Channel(
            "CCTV-4K",
            '#EXTINF:-1 group-title="大陆",CCTV-4K',
            "https://live-play-hls.cctvnews.cctv.com/CCTVChannel/channel_cctv4k_mbd.m3u8?auth_key=expired",
            "大陆",
        )
        cctv15 = builder.Channel(
            "CCTV-15 音乐",
            '#EXTINF:-1 group-title="大陆",CCTV-15 音乐',
            "https://xykt-fix.github.io/play/a02e/index.m3u8",
            "大陆",
        )
        self.assertFalse(builder.is_station_like(cctv4k))
        self.assertFalse(builder.is_station_like(cctv15))

    def test_core_metadata_is_canonical(self):
        channel = builder.Channel(
            "CCTV-5 体育",
            '#EXTINF:-1 group-title="大陆",CCTV-5 体育',
            "https://example.com/cctv5.m3u8",
            "大陆",
        )
        extinf = builder.cleaned_extinf(channel)
        self.assertIn('group-title="卫视台"', extinf)
        self.assertIn('tvg-id="CCTV5"', extinf)
        self.assertIn('tvg-name="CCTV5"', extinf)
        self.assertIn('/CCTV5.png"', extinf)
        self.assertTrue(extinf.endswith(",CCTV-5"))

    def test_playlist_header_advertises_epg(self):
        channel = builder.Channel(
            "北京卫视",
            '#EXTINF:-1 group-title="大陆",北京卫视',
            "https://example.com/beijing.m3u8",
            "大陆",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tv.m3u"
            builder.write_playlist(output, [channel], "fixture")
            text = output.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f'#EXTM3U x-tvg-url="{builder.EPG_URL}"'))
        self.assertIn('/%E5%8C%97%E4%BA%AC%E5%8D%AB%E8%A7%86.png"', text)


if __name__ == "__main__":
    unittest.main()
