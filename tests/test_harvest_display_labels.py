import unittest

from scripts.harvest_sources import parse_source
from scripts.harvest_incremental import normalize_rows
from scripts.build_home_candidate_manifest import build_manifest, DEFAULT_FORMAL_URL
from router.ac86u.home_contract import candidate_id


SOURCE = 'https://raw.githubusercontent.com/adminouyang/TV/master/output/ipv4/result.m3u'


class HarvestDisplayLabelsTest(unittest.TestCase):
    def test_display_labels_deduplicate_into_plain_url_candidates(self):
        text = ('#EXTM3U\n'
                '#EXTINF:-1,CCTV1\nhttps://example.com/new.m3u8$山东联通11\n'
                '#EXTINF:-1,CCTV1\nhttps://example.com/new.m3u8$订阅源\n')
        rows = normalize_rows(parse_source(text, '大陆', SOURCE))
        manifest, _ = build_manifest(
            discovery_rows=rows,
            formal_bytes=b'#EXTM3U\n#EXTINF:-1,CCTV1\nhttps://example.com/current.m3u8\n',
            formal_url=DEFAULT_FORMAL_URL, source_revision='test',
            generated_utc='2026-09-27T05:30:00Z')
        self.assertEqual(manifest['candidate_count'], 1)
        row = manifest['candidates'][0]
        self.assertEqual(row['request_options'], '')
        self.assertEqual(row['candidate_id'], candidate_id('cctv1', 'https://example.com/new.m3u8'))
        self.assertFalse(row['home_verified'])
        self.assertFalse(row['production_eligible'])

    def test_http_options_and_unknown_suffixes_are_preserved(self):
        for suffix in ('User-Agent=Custom&Referer=https://example.com',
                       '订阅源&Referer=https://example.com', '未知备注', '山东联通11|token=abc'):
            with self.subTest(suffix=suffix):
                rows = parse_source('#EXTINF:-1,CCTV1\nhttps://example.com/live$' + suffix,
                                    '大陆', SOURCE)
                self.assertEqual(rows[0]['options'], suffix)

    def test_other_upstreams_keep_existing_suffix_semantics(self):
        rows = parse_source('CCTV1,https://example.com/live$订阅源',
                            '大陆', 'https://example.com/list.txt')
        self.assertEqual(rows[0]['options'], '订阅源')


if __name__ == '__main__':
    unittest.main()
