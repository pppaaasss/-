import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apply_viewer_profile.py"
SPEC = importlib.util.spec_from_file_location("apply_viewer_profile", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def extinf(group: str, name: str) -> str:
    return f'#EXTINF:-1 group-title="{group}",{name}'


class ViewerProfileTests(unittest.TestCase):
    def test_removes_unwanted_groups_but_keeps_numbered_cctv(self):
        for group in ("纪录片", "中文纪录", "电影", "中文电影", "新闻", "国际", "韩国"):
            self.assertFalse(MODULE.keep_entry(extinf(group, "测试频道"))[0])
        self.assertTrue(MODULE.keep_entry(extinf("卫视台", "CCTV-9"))[0])
        self.assertTrue(MODULE.keep_entry(extinf("卫视台", "CCTV-13"))[0])

    def test_removes_english_services_even_when_misfiled(self):
        rejected = (
            ("中文综合", "VOA美国之音"),
            ("中文综合", "CNBC Asia"),
            ("香港", "ViuTVsix"),
            ("香港", "明珠台"),
            ("台湾", "Taiwan+"),
            ("日本", "NHK WORLD-JAPAN"),
            ("新加坡", "CNA亚洲新闻台"),
            ("卫视台", "CGTN纪录"),
        )
        for group, name in rejected:
            with self.subTest(group=group, name=name):
                self.assertFalse(MODULE.keep_entry(extinf(group, name))[0])

    def test_keeps_requested_regional_services(self):
        kept = (
            ("中文综合", "天津新闻频道"),
            ("台湾", "TVBS新闻台"),
            ("台湾", "Taiwan Indigenous TV"),
            ("日本", "JOTX-DTV"),
            ("日本", "NHK General TV"),
            ("香港", "TVB翡翠台"),
            ("香港", "RTHK TV 31"),
            ("新加坡", "Channel 8"),
            ("新加坡", "Channel U"),
        )
        for group, name in kept:
            with self.subTest(group=group, name=name):
                self.assertTrue(MODULE.keep_entry(extinf(group, name))[0])

    def test_reassigns_catch_all_chinese_group_to_regions(self):
        fixtures = (
            ("浙江钱江都市", "浙江"),
            ("苏州新闻综合", "江苏"),
            ("TVBS欢乐台", "台湾"),
            ("TVB翡翠台", "香港"),
            ("TOKYO MX チャンネル", "日本"),
            ("都市频道", "其他地方"),
        )
        for name, expected in fixtures:
            with self.subTest(name=name):
                entry = MODULE.Entry(
                    extinf("中文综合", name),
                    (extinf("中文综合", name), "https://example.com/live.m3u8"),
                )
                regionalized, old_group, new_group = MODULE.regionalize_entry(entry)
                self.assertEqual(old_group, "中文综合")
                self.assertEqual(new_group, expected)
                self.assertIn(f'group-title="{expected}"', regionalized.extinf)

    def test_rewrites_count_and_is_idempotent(self):
        playlist = "\n".join(
            (
                "#EXTM3U",
                "# channels=4",
                extinf("纪录片", "BBC Earth"),
                "https://example.com/doc.m3u8",
                extinf("卫视台", "CCTV-13"),
                "https://example.com/cctv13.m3u8",
                extinf("日本", "JOTX-DTV"),
                "https://example.com/jotx.m3u8",
                extinf("中文综合", "NBC News"),
                "https://example.com/nbc.m3u8",
                "",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.m3u"
            path.write_text(playlist, encoding="utf-8")
            first = MODULE.apply_profile(path)
            once = path.read_text(encoding="utf-8")
            second = MODULE.apply_profile(path)
            twice = path.read_text(encoding="utf-8")
        self.assertEqual(first["before"], 4)
        self.assertEqual(first["after"], 2)
        self.assertIn("# channels=2", once)
        self.assertEqual(once, twice)
        self.assertFalse(second["changed"])


if __name__ == "__main__":
    unittest.main()
