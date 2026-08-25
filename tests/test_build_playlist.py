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
        self.assertEqual(builder.TARGET_EASY, 200)
        self.assertEqual(builder.MIN_EASY, 55)

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

    def test_user_confirmed_guangdong_route_is_not_used_for_cctv15(self):
        channel = builder.Channel(
            "CCTV-15 音乐",
            '#EXTINF:-1 group-title="大陆",CCTV-15 音乐',
            "http://112.123.243.37:50085/tsfile/live/0016_1.m3u8?key=txiptv&playlive=0&authid=0",
            "大陆",
        )
        self.assertFalse(builder.is_station_like(channel))

    def test_jade_mislabelled_as_cctv4k_is_rejected(self):
        channel = builder.Channel(
            "CCTV-4K",
            '#EXTINF:-1 group-title="大陆",CCTV-4K',
            "http://r.jdshipin.com/krMB5",
            "大陆",
        )
        self.assertFalse(builder.is_station_like(channel))

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

    def test_route_history_ignores_rotating_auth_query(self):
        first = builder.Channel(
            "CCTV-1 综合",
            '#EXTINF:-1 group-title="大陆",CCTV-1 综合',
            "https://cdn.example/live/cctv1.m3u8?auth=old",
            "大陆",
        )
        second = builder.Channel(
            "CCTV-1 综合",
            '#EXTINF:-1 group-title="大陆",CCTV-1 综合',
            "https://cdn.example/live/cctv1.m3u8?auth=new",
            "大陆",
        )
        self.assertEqual(builder.route_history_key(first), builder.route_history_key(second))

    def test_history_rewards_repeated_success(self):
        reliable = builder.Channel(
            "北京卫视",
            '#EXTINF:-1 group-title="大陆",北京卫视',
            "https://example.com/beijing.m3u8",
            "大陆",
            history={"recent": [1, 1, 1, 1, 1]},
        )
        flaky = builder.Channel(
            "湖南卫视",
            '#EXTINF:-1 group-title="大陆",湖南卫视',
            "https://example.com/hunan.m3u8",
            "大陆",
            history={"recent": [1, 0, 1, 0, 0]},
        )
        self.assertGreater(builder.historical_score(reliable), builder.historical_score(flaky))

    def test_easy_gate_requires_three_checks_and_headroom(self):
        channel = builder.Channel(
            "CCTV-1 综合 1080p",
            '#EXTINF:-1 group-title="大陆",CCTV-1 综合 1080p',
            "https://dual.example/cctv1.m3u8",
            "大陆",
        )
        channel.probe = {
            "ok": True,
            "checks_ok": 3,
            "height": 1080,
            "segment_mbps": 8.0,
            "manifest_s": 1.0,
            "bandwidth": 4_000_000,
            "ipv4_dns": True,
            "ipv6_dns": True,
            "dual_stack_dns": True,
        }
        self.assertTrue(builder.is_easy_ready(channel))
        channel.probe["checks_ok"] = 2
        self.assertFalse(builder.is_easy_ready(channel))
        channel.probe["checks_ok"] = 3
        channel.probe["segment_mbps"] = 2.0
        self.assertFalse(builder.is_easy_ready(channel))

    def test_merge_probe_uses_worst_observed_network_values(self):
        first = {
            "ok": True,
            "segment_mbps": 12.0,
            "manifest_s": 0.8,
            "height": 1080,
            "stream_mbps": 5.5,
            "ipv4_dns": True,
        }
        later = {
            "ok": True,
            "segment_mbps": 7.5,
            "manifest_s": 1.6,
            "height": 1080,
            "stream_mbps": 4.8,
            "ipv6_dns": True,
        }
        merged = builder.merge_probe_results(first, later, checks_ok=3)
        self.assertEqual(merged["checks_ok"], 3)
        self.assertEqual(merged["segment_mbps"], 7.5)
        self.assertEqual(merged["manifest_s"], 1.6)
        self.assertEqual(merged["stream_mbps"], 4.8)
        self.assertTrue(merged["dual_stack_dns"])

    def test_media_segment_duration_is_parsed_for_bitrate_measurement(self):
        playlist = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:5.76,
segments/live-001.ts
"""
        url, duration = builder.first_media_segment(playlist, "https://cdn.example/live/index.m3u8")
        self.assertEqual(url, "https://cdn.example/live/segments/live-001.ts")
        self.assertAlmostEqual(duration, 5.76)

    def test_core_selection_prefers_higher_programme_bitrate(self):
        low = builder.Channel(
            "CCTV-10 科教",
            '#EXTINF:-1 group-title="大陆",CCTV-10 科教',
            "https://low.example/cctv10.m3u8",
            "大陆",
        )
        high = builder.Channel(
            "CCTV-10 科教",
            '#EXTINF:-1 group-title="大陆",CCTV-10 科教',
            "https://high.example/cctv10.m3u8",
            "大陆",
        )
        for channel, stream_mbps in ((low, 0.8), (high, 4.5)):
            channel.static_score = builder.channel_static_score(channel)
            channel.probe = {
                "ok": True,
                "checks_ok": 3,
                "height": 0,
                "segment_mbps": 10.0,
                "stream_mbps": stream_mbps,
                "manifest_s": 1.0,
            }
        self.assertGreater(builder.measured_score(high), builder.measured_score(low))

    def test_literal_ip_family_detection_is_offline_and_exact(self):
        self.assertEqual(builder.host_ip_families("192.0.2.1"), (True, False))
        self.assertEqual(builder.host_ip_families("2001:db8::1"), (False, True))

    def test_easy_list_keeps_usable_cctv_and_satellite_before_other_channels(self):
        cctv15 = builder.Channel(
            "CCTV-15 音乐 1080p",
            '#EXTINF:-1 group-title="大陆",CCTV-15 音乐 1080p',
            "http://192.0.2.15/live.m3u8",
            "大陆",
        )
        satellite = builder.Channel(
            "北京卫视 1080p",
            '#EXTINF:-1 group-title="大陆",北京卫视 1080p',
            "http://192.0.2.16/live.m3u8",
            "大陆",
        )
        ordinary = builder.Channel(
            "测试电视 1080p",
            '#EXTINF:-1 group-title="中文综合",测试电视 1080p',
            "https://dual.example/ordinary.m3u8",
            "中文综合",
        )
        for channel, speed, checks in (
            (cctv15, 1.0, 2),
            (satellite, 0.8, 2),
            (ordinary, 12.0, 3),
        ):
            channel.probe = {
                "ok": True,
                "checks_ok": checks,
                "height": 1080,
                "segment_mbps": speed,
                "manifest_s": 1.0,
                "bandwidth": 0,
            }
        self.assertFalse(builder.is_easy_ready(cctv15))
        selected = builder.select_easy([ordinary, cctv15, satellite], target=2)
        self.assertEqual(
            {builder.channel_key(channel) for channel in selected},
            {"cctv15", "北京卫视"},
        )
        self.assertTrue(all(channel.probe.get("easy_core_fallback") for channel in selected))


if __name__ == "__main__":
    unittest.main()
