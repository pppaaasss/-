"""Real engine/upload/publisher on copies of all four lists; media is simulated.

The verified route context belongs only to this isolated fixture. No household
route certification, public GitHub write, or TV playback is performed here.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.home_contract import ROUTE_CONTEXT, station_key
from router.ac86u.push_home_report import push
from scripts import publish_home_decisions as publisher
from scripts.build_home_candidate_manifest import build_manifest
from tests.test_home_first_shadow_integration import ROOT, FORMAL_URL, measured, report_remote, run_git


class FullPublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.originals = {n: (ROOT / n).read_bytes() for n in publisher.PRODUCTION_FILES}
        self.publish_root = self.root / 'publication'
        (self.publish_root / 'config').mkdir(parents=True)
        for name, raw in self.originals.items():
            (self.publish_root / name).write_bytes(raw)
        (self.publish_root / 'config/home-route-feedback.json').write_text('{"good":{},"bad":{}}')
        self.config_path = self.publish_root / 'config/home-publisher.json'
        self.config_path.write_text(json.dumps(dict(
            schema=publisher.CONFIG_SCHEMA, enabled=True, expected_probe_id='home-full-fixture',
            repository='pppaaasss/-', report_branch='home-reports', formal_playlist='tv-core.m3u',
            formal_playlist_url=FORMAL_URL, production_files=list(publisher.PRODUCTION_FILES),
            maximum_report_age_hours=18, exact_reported_route_only=True,
            branch_protection_required=True, home_feedback='config/home-route-feedback.json',
            receipt_path='home-publish/latest.json')))
        self.output = self.root / 'router-state'
        self.router_config = dict(
            probe_id='home-full-fixture', output_dir=str(self.output), playlist_url=FORMAL_URL,
            candidate_manifest_url='https://fixture.invalid/candidates.json',
            route_context=ROUTE_CONTEXT, actionable=True, minimum_headroom_ratio=1.35,
            maximum_load1=10000, minimum_mem_available_kib=1, github_push_enabled=True,
            github_repository='pppaaasss/-', github_report_branch='home-reports', git=shutil.which('git'))
        self.router_config_path = self.root / 'router.json'
        self.router_config_path.write_text(json.dumps(self.router_config))
        self.remote = report_remote(self.root)
        self.now = 1788285600.0
        self.routes = [(station_key(n), n, u) for n, u in home_probe.parse_playlist(self.originals['tv-core.m3u'])]
        self.keys = [k for k, _, _ in self.routes]
        self.backups = {k: 'https://fixture.invalid/backup/' + k for k in self.keys[:10]}

    def observe_run(self, kind, bad=(), unknown=(), unhealthy_backup=()):
        formal = (self.publish_root / 'tv-core.m3u').read_bytes()
        manifest, _ = build_manifest(
            discovery_rows=[dict(name=n, url=self.backups[k], sources=['isolated-fixture'])
                            for k, n, _ in self.routes if k in self.backups],
            formal_bytes=formal, formal_url=FORMAL_URL, source_revision='isolated-fixture',
            generated_utc=home_probe.utc_text(self.now))

        def observe(name, url, *, floor, **kwargs):
            key = station_key(name)
            is_backup = url in self.backups.values()
            if kind == 'recheck-1300':
                self.assertNotIn(url, self.backups.values(), 'afternoon requested a backup URL')
            row = measured(name, url, floor)
            if (is_backup and key in unhealthy_backup) or (not is_backup and key in bad):
                row.update(status='DEGRADED', observed_status='DEGRADED', headroom_ratio=.8,
                           min_download_mbps=4.8, error='headroom_0.800_below_1.350')
            if not is_backup and key in unknown:
                row.update(status='UNKNOWN', observed_status='UNKNOWN', error='incomplete_measurement')
            return row

        with mock.patch.object(home_probe, 'fetch_playlist', return_value=(formal, FORMAL_URL, .1)), \
             mock.patch.object(home_probe, 'fetch_candidate_manifest', return_value=(manifest, json.dumps(manifest).encode(), 'https://fixture.invalid/candidates')) as fetch, \
             mock.patch.object(home_probe, 'resource_check', return_value=('', {'mem_available_kib': 100000})), \
             mock.patch.object(home_probe, 'probe_route', side_effect=observe):
            report, _ = home_probe.run(self.router_config, run_kind=kind, now_epoch=self.now)
        if kind == 'recheck-1300':
            fetch.assert_not_called()
        self.assertTrue(push(self.router_config_path, self.output / 'latest.json',
                             remote_url_override=str(self.remote), transport_env_override=dict(os.environ)))
        checkout = self.root / ('checkout-' + str(int(self.now)))
        run_git(['clone', '--quiet', '--branch', 'home-reports', str(self.remote), str(checkout)])
        self.inbox = checkout / 'inbox'
        return report

    def publish(self):
        return publisher.publish_latest(root=self.publish_root, config_path=self.config_path,
                                        inbox=self.inbox, now_epoch=self.now + 60, apply=True)

    def bytes_now(self):
        return {n: (self.publish_root / n).read_bytes() for n in publisher.PRODUCTION_FILES}

    def test_all_channels_eight_swaps_four_lists_replay_and_next_run(self):
        # Eight replacements, one rejected backup, one missing backup, one UNKNOWN.
        bad = self.keys[:9] + [self.keys[10]]
        report = self.observe_run('primary-0200', bad=bad, unknown=[self.keys[11]],
                                  unhealthy_backup=[self.keys[8]])
        self.assertEqual(len(self.routes), report['summary']['channels'])
        self.assertEqual(8, report['summary']['replacements'])
        self.assertEqual(1, report['summary']['unknown'])
        result = self.publish()
        self.assertEqual('applied', result['status'])
        self.assertEqual(8, result['replacement_count'])
        swaps = {old: self.backups[k] for k, _, old in self.routes[:8]}
        for name, raw in self.originals.items():
            # Independent byte oracle: only exact URL lines may change; EXTINF,
            # ordering, channel count, other variants and all other bytes stay.
            expected = b''.join((swaps.get(line.rstrip(b'\r\n').decode(), '').encode() +
                                 line[len(line.rstrip(b'\r\n')):])
                                if line.rstrip(b'\r\n').decode() in swaps else line
                                for line in raw.splitlines(keepends=True))
            self.assertEqual(expected, self.bytes_now()[name], name)
        after = self.bytes_now()
        self.assertEqual('duplicate', self.publish()['status'])
        self.assertEqual(after, self.bytes_now())
        self.now += 24 * 3600
        second = self.observe_run('primary-0200')
        self.assertEqual(0, second['summary']['replacements'])
        self.assertEqual(len(self.routes), second['summary']['good'])
        self.assertEqual(0, self.publish()['replacement_count'])
        self.assertEqual(after, self.bytes_now())
        self.assertEqual(self.originals, {n: (ROOT / n).read_bytes() for n in publisher.PRODUCTION_FILES})

    def test_afternoon_replacement_uses_primary_cache_through_real_publisher(self):
        self.observe_run('primary-0200')
        self.assertEqual(0, self.publish()['replacement_count'])
        self.now += 11 * 3600
        report = self.observe_run('recheck-1300', bad=self.keys[:8])
        self.assertEqual(8, report['summary']['replacements'])
        self.assertTrue(all(r['purpose'] == 'primary-cache' for r in report['candidate_results']))
        self.assertEqual(8, self.publish()['replacement_count'])

    def test_mid_write_failure_restores_all_lists_and_existing_receipt_then_retry(self):
        self.observe_run('primary-0200')
        self.publish()
        previous = (self.publish_root / 'home-publish/latest.json').read_bytes()
        self.now += 11 * 3600
        self.observe_run('recheck-1300', bad=self.keys[:8])
        original_write = publisher.atomic_bytes
        calls = [0]

        def fail_once(path, raw):
            calls[0] += 1
            if calls[0] == 3:
                raise OSError('injected third playlist write failure')
            return original_write(path, raw)

        with mock.patch.object(publisher, 'atomic_bytes', side_effect=fail_once), \
             self.assertRaisesRegex(OSError, 'third playlist'):
            self.publish()
        self.assertEqual(self.originals, self.bytes_now())
        self.assertEqual(previous, (self.publish_root / 'home-publish/latest.json').read_bytes())
        self.assertEqual(8, self.publish()['replacement_count'])


if __name__ == '__main__':
    unittest.main()
