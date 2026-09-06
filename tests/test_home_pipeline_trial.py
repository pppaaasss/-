import contextlib
import hashlib
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from router.ac86u import home_probe as probe
from router.ac86u.home_contract import (
    CANDIDATE_SCHEMA, ContractError, make_candidate, object_sha256,
    validate_backup_pool, validate_home_report_v2,
)
from router.ac86u.home_decision import mass_failure_circuit
from tests.test_ac86u_home_probe import NOW, measured


FORMAL = b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/1\n#EXTINF:-1,CCTV-2\nhttps://current.test/2\n'


class PipelineTrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = dict(probe_id='home-ac86u-test', output_dir=str(self.root),
            route_context='router-origin-direct-wan', actionable=True, github_push_enabled=True,
            maximum_load1=10000, minimum_mem_available_kib=1,
            candidate_manifest_url='https://repo.test/candidates.json',
            playlist_url='https://repo.test/core.m3u', trial_candidate_file=str(self.root / 'candidates.json.gz'))
        candidates = [make_candidate(dict(name='CCTV-' + str(n), url='https://candidate.test/' + str(n),
                                         sources=['trial-test'])) for n in (1, 2)]
        manifest = dict(schema=CANDIDATE_SCHEMA, generated_utc=probe.utc_text(NOW),
            source_revision='trial-test', scope=['cctv', 'provincial_satellite'],
            formal_playlist=dict(url=self.config['playlist_url'], sha256=hashlib.sha256(FORMAL).hexdigest(), channel_count=2),
            cloud_stream_probe_performed=False, home_verified=False, production_eligible=False,
            candidate_count=2, candidate_set_sha256=object_sha256(candidates), candidates=candidates)
        (self.root / 'candidates.json.gz').write_bytes(gzip.compress(json.dumps(manifest).encode()))
        (self.root / 'state.json').write_text('{"formal": "unchanged"}')

    def run_trial(self, kind='primary-0200', resources=None, bad=False, missing_metadata=False):
        def observe(name, url, *, floor, **kwargs):
            row = measured(name, url, floor)
            if missing_metadata and 'current.test' in url:
                row['deep_checked'] = False
            if bad and url == 'https://current.test/1':
                row.update(status='DEGRADED', observed_status='DEGRADED', error='h264_stream_2.000_below_5.000_mbps')
                if bad == 'headroom':
                    row.update(headroom_ratio=.8, min_download_mbps=4.8, error='headroom_0.800_below_1.350')
            return row
        before = dict(self.config)
        with mock.patch.object(probe, 'fetch_playlist', return_value=(FORMAL, self.config['playlist_url'], 0.1)), \
             mock.patch.object(probe, 'fetch_candidate_manifest', side_effect=AssertionError('must use staged manifest')), \
             mock.patch.object(probe, 'resource_check', side_effect=resources, return_value=('', {'mem_available_kib': 100000})), \
             mock.patch.object(probe, 'probe_route', side_effect=observe) as calls, \
             contextlib.redirect_stdout(io.StringIO()):
            report, state = probe.run(self.config, trial=True, run_kind=kind, now_epoch=NOW + 60)
        self.assertEqual(before, self.config)
        self.assertEqual('{"formal": "unchanged"}', (self.root / 'state.json').read_text())
        self.assertFalse((self.root / 'latest.json').exists())
        self.assertFalse((self.root / 'qualified-backups.json').exists())
        validate_home_report_v2(report, trial=True)
        return report, state, calls

    def test_primary_retries_and_qualifies_then_recheck_uses_cache_without_candidate_requests(self):
        report, state, calls = self.run_trial(bad=True)
        self.assertEqual(2, sum(c.args[1] == 'https://current.test/1' for c in calls.call_args_list))
        self.assertEqual('BAD', report['current_results'][0]['status'])
        self.assertEqual(2, report['summary']['qualified_backups'])
        self.assertFalse(report['baseline']['route_verified'])
        self.assertFalse(report['actionable'])
        self.assertFalse(any(d['action'] == 'REPLACE' for d in report['decisions']))
        pool = json.loads((self.root / 'pipeline-trial/qualified-backups.json').read_text())
        validate_backup_pool(pool, trial=True)
        with self.assertRaises(ContractError):
            validate_home_report_v2(report)
        with self.assertRaises(ContractError):
            validate_backup_pool(pool)
        second, _, calls = self.run_trial('recheck-1300', bad=True)
        self.assertFalse(any('candidate.test' in c.args[1] for c in calls.call_args_list))
        self.assertEqual('not_requested', second['policy']['candidate_manifest_state'])
        self.assertEqual('primary-cache', second['candidate_results'][0]['purpose'])

    def test_confirmed_low_headroom_still_fails_quality_but_candidates_are_tested(self):
        self.config.update(circuit_breaker_min_unknown=1, circuit_breaker_unknown_ratio=.35,
                           minimum_headroom_ratio=1.35)
        report, _, calls = self.run_trial(bad='headroom')
        self.assertFalse(report['summary']['circuit_breaker_open'])
        self.assertEqual('BAD', report['current_results'][0]['status'])
        self.assertEqual('headroom_0.800_below_1.350', report['current_results'][0]['error'])
        self.assertEqual(2, report['summary']['candidate_confirmed'])
        self.assertTrue(any('candidate.test' in c.args[1] for c in calls.call_args_list))

    def test_resource_stop_saves_queue_and_next_run_does_not_restart_same_manifest(self):
        self.config['sample_actual_resources'] = True
        ok = ('', {'mem_available_kib': 100000})
        report, state, _ = self.run_trial(resources=[ok] * 4 + [('cpu_busy', {'cpu_busy_percent': 90})])
        self.assertEqual(1, report['summary']['qualified_backups'])
        self.assertEqual(1, len(state['candidate_queue']))
        report, state, calls = self.run_trial()
        self.assertEqual(1, sum('candidate.test' in c.args[1] for c in calls.call_args_list))
        self.assertEqual(2, report['summary']['qualified_backups'])
        self.assertEqual([], state['candidate_queue'])

    def test_trial_cannot_claim_verified_or_actionable_status(self):
        report, _, _ = self.run_trial()
        report['actionable'] = True
        with self.assertRaises(ContractError):
            validate_home_report_v2(report, trial=True)
        report['actionable'] = False
        report['baseline']['route_verified'] = True
        with self.assertRaises(ContractError):
            validate_home_report_v2(report, trial=True)

    def test_missing_quality_metadata_never_becomes_current_good(self):
        report, _, calls = self.run_trial(missing_metadata=True)
        self.assertEqual(2, report['summary']['unknown'])
        self.assertEqual(4, sum('current.test' in c.args[1] for c in calls.call_args_list))

    def test_unavailable_without_metadata_keeps_connection_failure_evidence(self):
        raw = measured('CCTV-1', 'https://current.test/1', 1080, 'UNAVAILABLE')
        raw.update(sample_count=0, deep_checked=False, error='HTTPError:HTTP Error 404: Not Found')
        with mock.patch.object(probe, 'probe_route', return_value=raw):
            row = probe._probe_current('CCTV-1', raw['url'], 'cctv1',
                profile=probe.run_profile('primary-0200', {}), config={})
        self.assertEqual('UNAVAILABLE', row['observed_status'])

    def test_actual_tv_binding_failure_stops_before_stream_tests(self):
        with mock.patch.object(probe, 'resource_check', return_value=('', {})), \
             mock.patch.object(probe, 'fetch_playlist', side_effect=[(FORMAL, '', 0),
                 (FORMAL.replace(b'https://current.test/1', b'https://different.test/1'), '', 0)]), \
             mock.patch.object(probe, 'probe_route') as calls:
            with self.assertRaisesRegex(RuntimeError, 'TV_CORE_ROUTE_BINDING_CHANGED'):
                probe.run(self.config, trial=True, now_epoch=NOW)
        calls.assert_not_called()

    def test_completed_low_quality_transfers_are_not_a_network_outage(self):
        quality = measured('CCTV-1', 'https://current.test/1', 1080, 'DEGRADED')
        quality['error'] = 'h264_stream_2.000_below_5.000_mbps'
        rows = {str(i): [quality, quality] for i in range(20)}
        self.assertFalse(mass_failure_circuit(rows, minimum_channels=12, failure_ratio=.35))
        quality['headroom_ratio'] = .8
        self.assertFalse(mass_failure_circuit(rows, minimum_channels=12, failure_ratio=.35))
        quality['observed_status'] = 'UNKNOWN'
        self.assertTrue(mass_failure_circuit(rows, minimum_channels=12, failure_ratio=.35))


if __name__ == '__main__':
    unittest.main()
