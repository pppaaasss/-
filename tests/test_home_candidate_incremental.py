import unittest
from pathlib import Path

from router.ac86u.home_contract import ContractError, validate_candidate_manifest
from scripts.build_home_candidate_manifest import (
    INDEX_SCHEMA,
    build_incremental_manifest,
    build_manifest,
)


FORMAL = (
    '#EXTM3U\n'
    '#EXTINF:-1 group-title="卫视台",CCTV-1\n'
    'http://current.test/cctv1.m3u8\n'
).encode()


def full(rows):
    return build_manifest(
        discovery_rows=rows,
        formal_bytes=FORMAL,
        formal_url='https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u',
        source_revision='abc123',
        generated_utc='2026-09-01T16:30:00Z',
    )[0]


def row(url='http://candidate.test/cctv1.m3u8', sources=None):
    return {
        'name': 'CCTV-1',
        'group': '大陆',
        'url': url,
        'sources': sources or ['source-a'],
    }


class HomeCandidateIncrementalTests(unittest.TestCase):
    def test_daily_workflow_finishes_before_the_0200_home_run(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/harvest-home-candidates.yml').read_text(encoding='utf-8')
        self.assertIn("cron: '30 16 * * *'", workflow)
        self.assertIn('--home-only', workflow)
        self.assertIn('hong_kong_evidence_consulted', workflow)
        self.assertIn('production-before.sha256', workflow)
        self.assertIn('production-after.sha256', workflow)
        self.assertNotIn('ffprobe', workflow)
        self.assertIn('gh pr create', workflow)
        self.assertIn('gh pr merge', workflow)
        self.assertIn('base_sha=', workflow)
        self.assertIn('--match-head-commit', workflow)
        self.assertIn('MASTER_RULES_API', workflow)
        self.assertIn('validate_master_rules', workflow)
        self.assertNotIn('git push origin HEAD:master', workflow)

    def test_first_run_bootstraps_without_dumping_historical_pool(self):
        delta, index, summary = build_incremental_manifest(full([row()]), None)
        self.assertEqual(0, delta['candidate_count'])
        self.assertEqual(1, index['candidate_count'])
        self.assertEqual(INDEX_SCHEMA, index['schema'])
        self.assertTrue(summary['bootstrap'])
        validate_candidate_manifest(delta)

    def test_only_new_or_changed_rows_are_emitted(self):
        _delta, index, _summary = build_incremental_manifest(full([row()]), None)
        unchanged, next_index, _summary = build_incremental_manifest(full([row()]), index)
        self.assertEqual(0, unchanged['candidate_count'])

        changed_source = row(sources=['source-a', 'source-b'])
        changed, next_index, _summary = build_incremental_manifest(full([changed_source]), next_index)
        self.assertEqual(1, changed['candidate_count'])

        added, _index, _summary = build_incremental_manifest(
            full([changed_source, row('http://new.test/cctv1.m3u8')]),
            next_index,
        )
        self.assertEqual(1, added['candidate_count'])
        self.assertEqual('http://new.test/cctv1.m3u8', added['candidates'][0]['url'])

    def test_disappeared_then_returned_route_is_new_again(self):
        _delta, index, _summary = build_incremental_manifest(full([row()]), None)
        empty, empty_index, summary = build_incremental_manifest(full([]), index)
        self.assertEqual(0, empty['candidate_count'])
        self.assertEqual(1, summary['disappeared'])
        returned, _index, _summary = build_incremental_manifest(full([row()]), empty_index)
        self.assertEqual(1, returned['candidate_count'])

    def test_corrupt_existing_index_fails_closed(self):
        bad = {'schema': INDEX_SCHEMA, 'candidate_digests': {'id': 'not-a-sha'}}
        with self.assertRaisesRegex(ContractError, 'index'):
            build_incremental_manifest(full([row()]), bad)

    def test_home_feedback_vetoes_exact_urls_and_repeated_hosts(self):
        rows = [
            row('http://bad.test/one.m3u8'),
            row('http://bad.test/two.m3u8'),
            row('http://good.test/live.m3u8'),
        ]
        manifest, summary = build_manifest(
            discovery_rows=rows,
            formal_bytes=FORMAL,
            formal_url='https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u',
            source_revision='abc123',
            generated_utc='2026-09-01T16:30:00Z',
            rejected_urls={'http://bad.test/one.m3u8'},
            rejected_hosts={'bad.test'},
        )
        self.assertEqual(['http://good.test/live.m3u8'], [item['url'] for item in manifest['candidates']])
        self.assertEqual(2, summary['rejected']['home_feedback'])

    def test_hong_kong_annotations_have_no_effect_on_candidate_identity(self):
        annotated = row()
        annotated['hk_verified'] = {'status': 'DEAD'}
        plain = row()
        self.assertEqual(full([plain])['candidates'], full([annotated])['candidates'])


if __name__ == '__main__':
    unittest.main()
