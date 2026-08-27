import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import organize_apptv_groups as module


class OrganizeGroupsTests(unittest.TestCase):
    def test_collapses_regions_and_moves_hunan_after_cctv(self):
        blocks = [
            module.Block('#EXTINF:-1 group-title="卫视台",CCTV-1', 'http://a/1.m3u8'),
            module.Block('#EXTINF:-1 group-title="卫视台",北京卫视', 'http://a/bj.m3u8'),
            module.Block('#EXTINF:-1 group-title="卫视台",CCTV-17', 'http://a/17.m3u8'),
            module.Block('#EXTINF:-1 group-title="湖南",长沙新闻', 'http://a/cs.m3u8'),
            module.Block('#EXTINF:-1 group-title="卫视台",湖南卫视', 'http://a/hn.m3u8'),
            module.Block('#EXTINF:-1 group-title="广东",广东珠江', 'http://a/gd.m3u8'),
            module.Block('#EXTINF:-1 group-title="中文付费",凤凰中文', 'http://a/pay.m3u8'),
        ]
        result = module.normalize_blocks(blocks)
        names = [module.visible_name(block.extinf) for block in result]
        self.assertEqual(names[:4], ["CCTV-1", "北京卫视", "CCTV-17", "湖南卫视"])
        groups = {module.visible_name(block.extinf): module.group_name(block.extinf) for block in result}
        self.assertEqual(groups["长沙新闻"], "地方台")
        self.assertEqual(groups["广东珠江"], "地方台")
        self.assertEqual(groups["湖南卫视"], "卫视台")
        self.assertEqual(groups["凤凰中文"], "中文付费")
        urls = {module.visible_name(block.extinf): block.url for block in result}
        self.assertEqual(urls["湖南卫视"], "http://a/hn.m3u8")

    def test_hunan_alias_url_is_preserved(self):
        original_url = "https://example.test/live/hunan.m3u8?token=fresh"
        result = module.normalize_blocks([
            module.Block('#EXTINF:-1 group-title="卫视台",湖南衛視', original_url),
        ])
        self.assertEqual(result[0].url, original_url)


if __name__ == "__main__":
    unittest.main()
