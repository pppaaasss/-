"""Deployment regressions found during the 2026-09-05 handover review."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from router.ac86u import github_pair, home_probe
from router.ac86u.activate import set_actionable
from router.ac86u.home_contract import ContractError, validate_home_report_v2
from router.ac86u.home_decision import current_result
from router.ac86u.push_home_report import push
from scripts.build_home_candidate_manifest import build_manifest
from tests.test_ac86u_github_push import master_rules, report, report_remote
from tests.test_ac86u_home_probe import NOW, measured, seed_backup_pool
from tests import test_home_publish

ROOT = Path(__file__).resolve().parents[1]
FORMAL = b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/cctv1.m3u8\n'


class DeploymentRegressions(unittest.TestCase):
    def config(self, temporary, **extra):
        return dict(probe_id='home-ac86u-test', output_dir=temporary,
                    maximum_load1=10000, minimum_mem_available_kib=1,
                    candidate_manifest_url='', actionable=True, **extra)

    def test_unknown_observations_never_become_confirmed_failure(self):
        unknown = measured('CCTV-1', 'https://test.invalid/1', 1080, 'UNKNOWN')
        bad = dict(unknown, observed_status='UNAVAILABLE')
        for attempts in ([unknown, unknown], [unknown, bad], [bad, unknown]):
            with self.subTest(attempts=attempts):
                result = current_result('CCTV-1', unknown['url'], attempts, circuit_open=False)
                self.assertEqual('UNKNOWN', result['status'])
                self.assertFalse(result['failure_confirmed'])
        self.assertEqual('BAD', current_result('CCTV-1', bad['url'], [bad, bad], circuit_open=False)['status'])

    def test_http_failure_and_internal_unknown_have_different_evidence(self):
        for error, expected in ((urllib.error.HTTPError('https://test.invalid', 404, 'missing', {}, None), 'UNAVAILABLE'),
                                (ValueError('unrecognized response'), 'UNKNOWN')):
            with mock.patch.object(home_probe, 'fetch_playlist', side_effect=error):
                result = home_probe.probe_route('CCTV-1', 'https://test.invalid', floor=1080, config={})
            self.assertEqual(expected, result['observed_status'])

    def test_disabled_publisher_can_pair_shadow_transport_without_bypassing_rules(self):
        publisher = json.loads((ROOT / 'config/home-publisher.json').read_text())
        self.assertFalse(publisher['enabled'])
        self.assertEqual('', publisher['expected_probe_id'])
        self.assertEqual(917, github_pair.validate_protected_publisher(publisher, master_rules(), {'probe_id':'home-ac86u-test'}))
        with self.assertRaises(RuntimeError):
            github_pair.validate_protected_publisher(publisher, [], {'probe_id':'home-ac86u-test'})

    def test_afternoon_uses_cache_and_never_requests_backup_even_when_bad(self):
        def formal_only(name, url, *, floor, **kwargs):
            self.assertIn('current.test', url, '13:00 must not request any backup URL')
            return measured(name, url, floor, 'DEGRADED')
        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, FORMAL, [1])
            with mock.patch.object(home_probe, 'fetch_playlist', return_value=(FORMAL, 'https://repo.test/core', .1)), \
                 mock.patch.object(home_probe, 'probe_route', side_effect=formal_only) as probe, \
                 mock.patch.object(home_probe, 'fetch_candidate_manifest') as fetch:
                result, _ = home_probe.run(self.config(temporary), run_kind='recheck-1300', now_epoch=NOW + 11*3600)
            self.assertEqual(2, probe.call_count)
            fetch.assert_not_called()
            self.assertEqual('REPLACE', result['decisions'][0]['action'])
            evidence = result['candidate_results'][0]
            self.assertEqual('primary-cache', evidence['purpose'])
            self.assertFalse(evidence['switch_reverified'])
            self.assertEqual(home_probe.utc_text(NOW), evidence['last_verified_utc'])
            validate_home_report_v2(result, now_epoch=NOW + 11*3600)
            with self.assertRaisesRegex(ContractError, 'expired'):
                validate_home_report_v2(result, now_epoch=NOW + 37*3600)

    def test_afternoon_expired_cache_is_not_probed_or_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, FORMAL, [1])
            with mock.patch.object(home_probe, 'fetch_playlist', return_value=(FORMAL, 'https://repo.test/core', .1)), \
                 mock.patch.object(home_probe, 'probe_route', side_effect=lambda name,url,*,floor,**kw: measured(name,url,floor,'DEGRADED')) as probe:
                result, _ = home_probe.run(self.config(temporary), run_kind='recheck-1300', now_epoch=NOW + 37*3600)
            self.assertEqual(2, probe.call_count)
            self.assertEqual('UNRESOLVED', result['decisions'][0]['action'])
            self.assertEqual([], result['candidate_results'])

    def test_primary_repair_precedes_discovery_and_survives_candidate_budget_exhaustion(self):
        manifest, _ = build_manifest(
            discovery_rows=[{'name':'CCTV-1', 'url':f'https://new.test/{i}', 'sources':['test']} for i in range(2)],
            formal_bytes=FORMAL, formal_url='https://repo.test/core', source_revision='test', generated_utc=home_probe.utc_text(NOW))
        calls = []
        clock = [0.0]
        def probe(name, url, *, floor, **kwargs):
            calls.append(url)
            clock[0] += 1300 if 'new.test' in url else 1
            return measured(name,url,floor,'DEGRADED' if 'current.test' in url else 'GOOD')
        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, FORMAL, [1])
            config = self.config(temporary)
            config['candidate_manifest_url'] = 'https://repo.test/candidates'
            with mock.patch.object(home_probe, 'fetch_playlist', return_value=(FORMAL, 'https://repo.test/core', .1)), \
                 mock.patch.object(home_probe, 'fetch_candidate_manifest', return_value=(manifest,b'manifest','https://repo.test/candidates')), \
                 mock.patch.object(home_probe, 'probe_route', side_effect=probe), \
                 mock.patch.object(home_probe.time, 'monotonic', side_effect=lambda:clock[0]):
                result, state = home_probe.run(config, now_epoch=NOW)
            self.assertIn('candidate.test', calls[2])
            self.assertIn('new.test', calls[3])
            self.assertEqual('REPLACE', result['decisions'][0]['action'])
            self.assertEqual(1, len(state['candidate_queue']))

    def test_viewer_veto_of_current_route_overrides_automatic_good(self):
        feedback = {'bad':{'cctv1':[{'url':'https://current.test/cctv1.m3u8','reason':'wrong_channel'}]}}
        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, FORMAL, [1])
            with mock.patch.object(home_probe, 'fetch_playlist', return_value=(FORMAL, 'https://repo.test/core', .1)), \
                 mock.patch.object(home_probe, 'probe_route', side_effect=lambda name,url,*,floor,**kw: measured(name,url,floor)):
                result, _ = home_probe.run(self.config(temporary, home_feedback=feedback), run_kind='recheck-1300', now_epoch=NOW+11*3600)
            self.assertEqual('BAD', result['current_results'][0]['status'])
            self.assertEqual('viewer_confirmed_bad_route', result['current_results'][0]['error'])
            self.assertEqual('REPLACE', result['decisions'][0]['action'])

    def test_four_distinct_uploaded_observations_are_required_for_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = report_remote(root)
            output = root/'state'; output.mkdir()
            config_path = root/'config.json'
            config = self.config(str(output))
            config.update(github_push_enabled=True, protected_publishing_ready=True,
                          github_repository='pppaaasss/-', github_report_branch='home-reports', git=shutil.which('git'),
                          route_context='living-room-path-equivalent', actionable=False)
            config_path.write_text(json.dumps(config))
            latest = output/'latest.json'
            for hour in (0,0,6,12,18):
                latest.write_text(json.dumps(report(generated=f'2026-09-02T{hour:02d}:00:00Z')))
                push(config_path, latest, remote_url_override=str(remote), transport_env_override=dict(os.environ))
                if hour==0:
                    state=json.loads((output/'github-state.json').read_text())
                    self.assertEqual(1,state['successful_reports'])
                    with self.assertRaisesRegex(RuntimeError,'shadow window'):
                        set_actionable(config_path,enabled=True,now_epoch=1_788_372_000)
            import calendar
            epoch=calendar.timegm(home_probe.time.strptime('2026-09-02T18:10:00Z','%Y-%m-%dT%H:%M:%SZ'))
            enabled=set_actionable(config_path,enabled=True,now_epoch=epoch)
            self.assertTrue(enabled['actionable'])
            self.assertEqual(4,json.loads((output/'github-state.json').read_text())['successful_reports'])

    def test_actual_installer_in_isolated_opt_installs_activation_and_shadow_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); opt=root/'opt'; bin_dir=opt/'bin'; bin_dir.mkdir(parents=True)
            hooks=root/'jffs/scripts'; hooks.mkdir(parents=True)
            (hooks/'services-start').write_text('#!/bin/sh\n# Existing ShellClash startup remains\n')
            for name, executable in [('python3', shutil.which('python3')), ('ssh-keygen',shutil.which('ssh-keygen'))]:
                (bin_dir/name).symlink_to(executable)
            stubs={
                'opkg':'exit 0', 'id':'echo 0', 'nvram':'echo test-router',
                'df':'printf "Filesystem 1024-blocks Used Available Capacity Mounted\\nusb 64000000 0 64000000 0%% /opt\\n"',
                'cru':'printf "%s\\n" "$*" >> "$TEST_CRON_LOG"',
                'curl':'for arg in "$@"; do case "$arg" in https://*) name=${arg##*/} ;; esac; done\nwhile [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then cp "$TEST_SOURCE/$name" "$2"; exit; fi; shift; done\nexit 1',
            }
            for name,body in stubs.items():
                path=bin_dir/name;path.write_text('#!/bin/sh\n'+body+'\n');path.chmod(0o755)
            installer=root/'install.sh'
            installer.write_text((ROOT/'router/ac86u/install.sh').read_text().replace('/opt',str(opt)).replace('/jffs',str(root/'jffs')))
            env=dict(os.environ,PATH=str(bin_dir)+':'+os.environ['PATH'], TEST_SOURCE=str(ROOT/'router/ac86u'),
                     TEST_CRON_LOG=str(root/'cron.log'),IPTV_HOME_SKIP_INITIAL_RUN='1')
            process=subprocess.run(['sh',str(installer)],env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(0,process.returncode,process.stdout+process.stderr)
            self.assertTrue((opt/'share/iptv-home-probe/activate.sh').is_file())
            cfg=json.loads((opt/'etc/iptv-home-probe.json').read_text())
            self.assertFalse(cfg['actionable']); self.assertFalse(cfg['github_push_enabled'])
            self.assertEqual(1200,cfg['maximum_runtime_s'])
            self.assertIn('Existing ShellClash startup remains',(hooks/'services-start').read_text())
            self.assertIn('0 2 * * *',(root/'cron.log').read_text())
            self.assertIn('0 13 * * *',(root/'cron.log').read_text())


class PublicationRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture=test_home_publish.HomePublishTests()
        self.fixture.setUp()
    def tearDown(self):
        self.fixture.tearDown()
    def cached_report(self):
        report=self.fixture.report()
        report['run_kind']='recheck-1300'
        for row in report['candidate_results']:
            row.update(purpose='primary-cache',switch_reverified=False,verified_run_kind='primary-0200',
                       last_verified_utc='2026-09-02T02:00:00Z',expires_utc='2026-09-03T14:00:00Z')
        return report
    def test_cached_primary_evidence_can_publish_exact_afternoon_replacement(self):
        self.fixture.queue(self.cached_report())
        result=self.fixture.publish()
        self.assertEqual('applied',result['status'])
        self.assertEqual(1,result['replacement_count'])
    def test_disabled_publisher_validates_shadow_without_any_write(self):
        self.fixture.queue(self.cached_report())
        self.fixture.write_config(enabled=False)
        originals={p:p.read_bytes() for p in self.fixture.root.glob('*.m3u')}
        from scripts.publish_home_decisions import publish_latest
        result=publish_latest(root=self.fixture.root,config_path=self.fixture.config_path,inbox=self.fixture.inbox,
                              now_epoch=self.fixture.now,apply=True,inspect_shadow=True)
        self.assertEqual('shadow',result['status'])
        self.assertEqual(originals,{p:p.read_bytes() for p in originals})
        self.assertFalse((self.fixture.root/'home-publish/latest.json').exists())
    def test_afternoon_rejects_live_reverification_and_unproven_cache(self):
        for change in ({'purpose':'switch-reverification','switch_reverified':True},
                       {'verified_run_kind':'recheck-1300'}, {'expires_utc':'2026-09-02T12:00:00Z'}):
            report=self.cached_report();report['candidate_results'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_home_report_v2(report)
