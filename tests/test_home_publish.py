import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import publish_home_decisions as publisher_module
from router.ac86u.home_contract import (
    REPORT_SCHEMA,
    ROUTE_CONTEXT,
    candidate_id,
    canonical_name,
    url_sha256,
)
from router.ac86u.push_home_report import report_filename
from scripts.publish_home_decisions import CONFIG_SCHEMA, PRODUCTION_FILES, publish_latest


UTC = timezone.utc
FORMAL_URL = 'https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u'


def verification(*, good=True, deep=False):
    return {
        'sample_count': 2,
        'startup_s': 0.8 if good else 30.0,
        'min_download_mbps': 20.0 if good else 0.0,
        'stream_mbps': 6.0 if good else 0.0,
        'headroom_ratio': 3.333 if good else 0.0,
        'width': 1920 if good else 0,
        'height': 1080 if good else 0,
        'codec': 'h264' if good else '',
        'fps': 50.0 if good else 0.0,
        'bitrate_mbps': 6.0 if good else 0.0,
        'deep_checked': deep,
    }


def playlist(routes):
    lines = ['#EXTM3U']
    for key, url in routes:
        lines.extend((f'#EXTINF:-1 group-title="卫视台",{canonical_name(key)}', url))
    return ('\n'.join(lines) + '\n').encode()


class HomePublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        self.probe_id = 'home-ac86u-test'
        self.now = datetime(2026, 9, 2, 13, 10, tzinfo=UTC).timestamp()
        self.routes = [('cctv1', 'https://current.test/cctv1.m3u8'), ('cctv2', 'https://current.test/cctv2.m3u8')]
        formal = playlist(self.routes)
        for name in PRODUCTION_FILES:
            raw = formal
            if name == 'tv-easy.m3u':
                raw = playlist([
                    ('cctv1', 'https://alternate.test/cctv1.m3u8'),
                    ('cctv2', 'https://current.test/cctv2.m3u8'),
                ])
            (self.root / name).write_bytes(raw)
        self.config_path = self.root / 'config/home-publisher.json'
        self.write_config(enabled=True)
        (self.root / 'config/home-route-feedback.json').write_text(
            json.dumps({'good': {}, 'bad': {}}), encoding='utf-8'
        )
        self.inbox = self.root / 'inbox'

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self, *, enabled):
        self.config_path.write_text(json.dumps({
            'schema': CONFIG_SCHEMA,
            'enabled': enabled,
            'expected_probe_id': self.probe_id if enabled else '',
            'repository': 'pppaaasss/-',
            'report_branch': 'home-reports',
            'formal_playlist': 'tv-core.m3u',
            'formal_playlist_url': FORMAL_URL,
            'production_files': list(PRODUCTION_FILES),
            'maximum_report_age_hours': 18,
            'exact_reported_route_only': True,
            'branch_protection_required': True,
            'home_feedback': 'config/home-route-feedback.json',
            'receipt_path': 'home-publish/latest.json',
        }), encoding='utf-8')

    def report(self, *, replace_keys=('cctv1',), generated='2026-09-02T13:00:00Z'):
        formal = (self.root / 'tv-core.m3u').read_bytes()
        current = []
        candidates = []
        decisions = []
        replace = set(replace_keys)
        for key, url in self.routes:
            bad = key in replace
            current.append({
                'channel_key': key,
                'name': canonical_name(key),
                'url': url,
                'url_sha256': url_sha256(url),
                'status': 'BAD' if bad else 'GOOD',
                'failure_confirmed': bad,
                'attempt_count': 2 if bad else 1,
                'verification': verification(good=not bad),
            })
            identity = None
            if bad:
                new_url = f'https://backup.test/{key}.m3u8'
                identity = candidate_id(key, new_url, '')
                candidates.append({
                    'candidate_id': identity,
                    'channel_key': key,
                    'url': new_url,
                    'request_options': '',
                    'qualification': 'QUALIFIED',
                    'purpose': 'switch-reverification',
                    'switch_reverified': True,
                    'verification': verification(good=True, deep=True),
                })
            decisions.append({
                'channel_key': key,
                'action': 'REPLACE' if bad else 'KEEP',
                'reason': 'confirmed_home_failure' if bad else 'healthy_home_route',
                'replacement_candidate_id': identity,
            })
        return {
            'schema': REPORT_SCHEMA,
            'probe_id': self.probe_id,
            'generated_utc': generated,
            'run_kind': 'recheck-1300',
            'run_status': 'COMPLETED',
            'production_modified': False,
            'actionable': True,
            'route_context': ROUTE_CONTEXT,
            'formal_playlist': {
                'url': FORMAL_URL,
                'sha256': hashlib.sha256(formal).hexdigest(),
                'channel_count': len(self.routes),
            },
            'baseline': {
                'home_network_ok': True,
                'github_reachable': True,
                'route_verified': True,
                'mass_failure_circuit_breaker': False,
            },
            'current_results': current,
            'candidate_results': candidates,
            'decisions': decisions,
            'summary': {'circuit_breaker_open': False},
        }

    def queue(self, report):
        raw = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
        directory = self.inbox / self.probe_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / report_filename(report, raw)
        path.write_bytes(raw)
        return path

    def publish(self):
        return publish_latest(
            root=self.root,
            config_path=self.config_path,
            inbox=self.inbox,
            now_epoch=self.now,
            apply=True,
        )

    def urls(self, name):
        return [
            line for line in (self.root / name).read_text(encoding='utf-8').splitlines()
            if line.startswith(('http://', 'https://'))
        ]

    def test_replaces_only_the_exact_reported_route_in_one_git_snapshot(self):
        self.queue(self.report())
        result = self.publish()
        self.assertEqual('applied', result['status'])
        self.assertEqual(1, result['replacement_count'])
        self.assertIn('https://backup.test/cctv1.m3u8', self.urls('tv-core.m3u'))
        self.assertIn('https://backup.test/cctv1.m3u8', self.urls('tv.m3u'))
        self.assertIn('https://backup.test/cctv1.m3u8', self.urls('tv-all.m3u'))
        self.assertIn('https://alternate.test/cctv1.m3u8', self.urls('tv-easy.m3u'))
        self.assertNotIn('https://backup.test/cctv1.m3u8', self.urls('tv-easy.m3u'))
        self.assertIn('https://current.test/cctv2.m3u8', self.urls('tv-core.m3u'))
        receipt = json.loads((self.root / 'home-publish/latest.json').read_text(encoding='utf-8'))
        self.assertTrue(receipt['policy']['good_routes_remain_untouched'])
        self.assertIsNone(receipt['policy']['replacement_count_limit'])

    def test_duplicate_report_is_idempotent_after_the_playlist_changes(self):
        self.queue(self.report())
        self.publish()
        before = {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES}
        result = self.publish()
        self.assertEqual('duplicate', result['status'])
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES})

    def test_zero_replacements_still_records_one_idempotent_receipt(self):
        self.queue(self.report(replace_keys=()))
        result = self.publish()
        self.assertEqual(0, result['replacement_count'])
        self.assertTrue((self.root / 'home-publish/latest.json').is_file())
        self.assertEqual('duplicate', self.publish()['status'])

    def test_stale_formal_hash_rejects_without_further_changes(self):
        self.queue(self.report())
        path = self.root / 'tv-core.m3u'
        path.write_bytes(path.read_bytes() + b'# changed\n')
        before = {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES}
        with self.assertRaisesRegex(RuntimeError, 'different formal'):
            self.publish()
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES})

    def test_stale_report_rejects_without_writing_a_receipt(self):
        self.queue(self.report(generated='2026-09-01T18:00:00Z'))
        before = {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES}
        with self.assertRaisesRegex(Exception, 'stale'):
            self.publish()
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES})
        self.assertFalse((self.root / 'home-publish/latest.json').exists())

    def test_transaction_rolls_back_all_playlists_if_receipt_write_fails(self):
        self.queue(self.report())
        before = {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES}
        with mock.patch.object(publisher_module, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.publish()
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES})
        self.assertFalse((self.root / 'home-publish/latest.json').exists())

    def test_home_feedback_can_veto_an_automatically_qualified_backup(self):
        report = self.report()
        self.queue(report)
        (self.root / 'config/home-route-feedback.json').write_text(json.dumps({
            'good': {},
            'bad': {'cctv1': [{'url': 'https://backup.test/cctv1.m3u8'}]},
        }), encoding='utf-8')
        before = {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES}
        with self.assertRaisesRegex(RuntimeError, 'feedback vetoes'):
            self.publish()
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in PRODUCTION_FILES})

    def test_unknown_cannot_be_forged_into_a_replacement(self):
        report = self.report()
        report['current_results'][0]['status'] = 'UNKNOWN'
        report['current_results'][0]['failure_confirmed'] = False
        report['current_results'][0]['attempt_count'] = 1
        self.queue(report)
        with self.assertRaisesRegex(Exception, 'confirmed home evidence'):
            self.publish()

    def test_replacement_count_has_no_channel_cap(self):
        self.routes = [(f'cctv{index}', f'https://current.test/cctv{index}.m3u8') for index in range(1, 9)]
        formal = playlist(self.routes)
        for name in PRODUCTION_FILES:
            (self.root / name).write_bytes(formal)
        self.queue(self.report(replace_keys=tuple(key for key, _url in self.routes)))
        result = self.publish()
        self.assertEqual(8, result['replacement_count'])
        for index in range(1, 9):
            self.assertIn(f'https://backup.test/cctv{index}.m3u8', self.urls('tv-core.m3u'))

    def test_two_reports_with_one_timestamp_fail_closed(self):
        report = self.report()
        first = self.queue(report)
        changed = json.loads(json.dumps(report))
        changed['decisions'][0]['reason'] = 'same_time_changed_content'
        second = self.queue(changed)
        self.assertNotEqual(first.name, second.name)
        with self.assertRaisesRegex(RuntimeError, 'same latest timestamp'):
            self.publish()

    def test_disabled_publisher_never_needs_an_inbox(self):
        self.write_config(enabled=False)
        result = self.publish()
        self.assertEqual('disabled', result['status'])
        self.assertFalse((self.root / 'home-publish/latest.json').exists())

    def test_workflow_uses_only_home_reports_and_the_shared_production_lock(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/publish-home-decisions.yml').read_text(encoding='utf-8')
        self.assertIn("cron: '25 18 * * *'", workflow)
        self.assertIn("cron: '25 5 * * *'", workflow)
        self.assertIn('group: production-publish-lock', workflow)
        self.assertIn('refs/heads/home-reports', workflow)
        self.assertIn('scripts/publish_home_decisions.py', workflow)
        self.assertIn('git status --porcelain -- tv-easy.m3u', workflow)
        self.assertIn('gh pr create', workflow)
        self.assertIn('gh pr merge', workflow)
        self.assertIn('base_sha=', workflow)
        self.assertIn('--match-head-commit', workflow)
        self.assertIn('MASTER_RULES_API', workflow)
        self.assertIn('validate_master_rules', workflow)
        self.assertNotIn('git push origin HEAD:master', workflow)
        self.assertNotIn('health-monitor', workflow)
        self.assertNotIn('Hong Kong', workflow)


if __name__ == '__main__':
    unittest.main()
