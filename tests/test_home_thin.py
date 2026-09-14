import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
import unittest
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.home_transport import DownloadBudget
from router.ac86u.thin_contract import (encode, epoch, timestamp, digest, RESULT_SCHEMA,
    validate_task, validate_observations)
from router.ac86u.thin_probe import reserve, settle
from router.ac86u.thin_api import GithubData, API
from scripts import home_thin_control as cloud
from scripts.build_home_candidate_manifest import build_manifest
from scripts.publish_home_decisions import publish_latest


def measured(task, status='GOOD'):
    return {'url': task['url'], 'channel_key': task['channel_key'], 'observed_status': status,
        'sample_count': 2 if status == 'GOOD' else 0, 'startup_s': 1.0,
        'min_download_mbps': 20.0 if status == 'GOOD' else 0,
        'stream_mbps': 6.0 if status == 'GOOD' else 0, 'headroom_ratio': 3.333 if status == 'GOOD' else 0,
        'width': 1920 if status == 'GOOD' else 0, 'height': 1080 if status == 'GOOD' else 0,
        'codec': 'h264' if status == 'GOOD' else '', 'fps': 50 if status == 'GOOD' else 0,
        'bitrate_mbps': 6.0 if status == 'GOOD' else 0, 'deep_checked': status == 'GOOD', 'error': '',
        'segment_samples': [{'downloaded_bytes': 1024*1024, 'elapsed_s': 0.42}] * 2 if status == 'GOOD' else []}


class ThinFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'config').mkdir()
        (self.root/'harvest').mkdir()
        self.reports = self.root/'reports'
        self.reports.mkdir()
        self.now = epoch('2026-09-14T18:05:00Z')
        self.config = json.loads((Path(__file__).resolve().parents[1]/'config/home-thin.json').read_bytes())
        self.config['enabled'] = True
        self.probe = self.config['probe_id']
        self.state = cloud.new_state(self.probe)
        self.formal = b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttp://current.test/one.m3u8\n#EXTINF:-1,CCTV-2\nhttp://current.test/two.m3u8\n'
        for name in ('tv-core.m3u','tv.m3u','tv-all.m3u','tv-easy.m3u'):
            (self.root/name).write_bytes(self.formal)
        (self.root/'config/home-route-feedback.json').write_text('{"good":{},"bad":{}}')
        publisher = json.loads((Path(__file__).resolve().parents[1]/'config/home-publisher.json').read_bytes())
        (self.root/'config/home-publisher.json').write_text(json.dumps(publisher))
        self.manifest, _ = build_manifest(discovery_rows=[{'name':'CCTV-1','url':'http://candidate.test/one.m3u8', 'sources':['fixture']}],
            formal_bytes=self.formal, formal_url=publisher['formal_playlist_url'], source_revision='a'*40,
            generated_utc=timestamp(self.now))
        (self.root/'harvest/home-candidates.json').write_bytes(encode(self.manifest))

    def tearDown(self):
        self.tmp.cleanup()

    def migrated(self):
        self.state = cloud.import_legacy(self.state, {'state.json': b'{"tested_candidate_ids":[]}'}, 'migration')

    def step(self, seconds=1):
        self.now += seconds
        self.state, task, report = cloud.step(self.state, self.config, self.root, self.reports, self.now)
        return task, report

    def deliver(self, task, statuses=None, subset=None, stop=''):
        started = self.now + 1
        selected = task['tasks'] if subset is None else task['tasks'][:subset]
        rows = [{'task_id': row['task_id'], 'started_utc': timestamp(started), 'finished_utc': timestamp(started+1),
                 'result': measured(row, (statuses or {}).get(row['channel_key'], 'GOOD'))} for row in selected]
        value = {'schema': RESULT_SCHEMA, 'probe_id': self.probe, 'batch_id': task['batch_id'],
            'cycle_id': task['cycle_id'], 'route_context': task['route_context'],
            'started_utc': timestamp(started), 'finished_utc': timestamp(started+1), 'results': rows,
            'usage': {'downloaded_bytes': 1024 * 1024 if rows else 0, 'runtime_s': 2}, 'stop_reason': stop}
        path = self.reports/'observations'/self.probe/(task['batch_id']+'.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode(value))
        self.now += 3
        return value


class ThinFlowTests(ThinFixture):
    def test_migration_required_before_any_probe(self):
        task, report = self.step()
        self.assertEqual('WAITING_HISTORY_MIGRATION', task['state'])
        self.assertIsNone(report)
        self.assertFalse(self.state['batches'])

    def test_legacy_history_keeps_exact_timestamps_and_deduplication(self):
        row = self.manifest['candidates'][0]
        old = {'tested_candidate_ids':[row['candidate_id']], 'candidate_queue':[row],
               'candidate_observations':{row['candidate_id']:{'last_checked_utc':'2026-09-07T01:00:00Z'}}}
        self.state = cloud.import_legacy(self.state, {'state.json':encode(old)}, 'sha')
        self.assertEqual('2026-09-07T01:00:00Z', self.state['tested'][row['candidate_id']]['last_checked_utc'])
        self.assertNotIn(row['candidate_id'], self.state['queue'])
        task, _ = self.step()
        self.deliver(task)
        _, report = self.step()
        self.assertIsNotNone(report)
        self.assertFalse(report['candidate_results'])

    def test_corrupt_migration_does_not_reset_history(self):
        with self.assertRaises((ValueError, UnicodeError)):
            cloud.import_legacy(self.state, {'state.json': b'\x00broken'}, 'sha')
        self.assertFalse(self.state['migration_complete'])

    def test_full_home_to_cloud_to_protected_publication(self):
        self.migrated()
        first, report = self.step()
        self.assertIsNone(report)
        self.assertTrue(all(t['role']=='current' for t in first['tasks']))
        self.deliver(first, {'cctv1':'UNAVAILABLE'})
        second, report = self.step()
        self.assertEqual([2], [t['attempt'] for t in second['tasks']])
        self.assertIsNone(report)
        self.deliver(second, {'cctv1':'UNAVAILABLE'})
        candidate, report = self.step()
        self.assertEqual(['candidate'], [t['role'] for t in candidate['tasks']])
        self.deliver(candidate)
        _, report = self.step()
        self.assertEqual(1, report['summary']['replacements'])
        self.assertEqual('cloud_from_household_measurements', report['policy']['decision_origin'])
        self.assertFalse(report['policy']['cloud_stream_probe_performed'])
        raw = encode(report)
        path = self.reports/'inbox'/self.probe/cloud.report_filename(report, raw)
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        result = publish_latest(root=self.root, config_path=self.root/'config/home-publisher.json',
            inbox=self.reports/'inbox', now_epoch=self.now, apply=True)
        self.assertEqual(1, result['replacement_count'])
        self.assertIn(b'http://candidate.test/one.m3u8', (self.root/'tv-core.m3u').read_bytes())
        self.assertIn(b'http://current.test/two.m3u8', (self.root/'tv-core.m3u').read_bytes())

    def test_partial_batch_never_becomes_full_report(self):
        self.migrated()
        first, _ = self.step()
        self.deliver(first, subset=1, stop='memory_reserve')
        second, report = self.step()
        self.assertIsNone(report)
        self.assertEqual(1, len(second['tasks']))
        self.assertEqual('cctv2', second['tasks'][0]['channel_key'])

    def test_resource_pause_is_visible_to_cloud_without_bad_routes(self):
        self.migrated()
        task, _ = self.step()
        self.deliver(task, subset=0, stop='memory_reserve')
        idle, report = self.step()
        self.assertEqual('WAITING_HOME_RESOURCES', idle['state'])
        self.assertEqual('memory_reserve', self.state['last_heartbeat']['stop_reason'])
        self.assertFalse(self.state['cycle']['results'])
        self.assertIsNone(report)

    def test_unknown_twice_does_not_replace(self):
        self.migrated()
        task, _ = self.step()
        self.deliver(task, {'cctv1':'UNKNOWN'})
        task, _ = self.step()
        self.deliver(task, {'cctv1':'UNKNOWN'})
        task, _ = self.step()
        self.deliver(task)
        _, report = self.step()
        self.assertEqual('UNKNOWN', next(r for r in report['current_results'] if r['channel_key']=='cctv1')['status'])
        self.assertEqual(0, report['summary']['replacements'])

    def test_afternoon_never_schedules_candidates_or_backups(self):
        self.migrated()
        self.now = epoch('2026-09-15T05:05:00Z')
        task, _ = self.step()
        self.deliver(task, {'cctv1':'UNAVAILABLE'})
        task, _ = self.step()
        self.assertTrue(all(t['role']=='current' for t in task['tasks']))
        self.deliver(task, {'cctv1':'UNAVAILABLE'})
        _, report = self.step()
        self.assertEqual(0, report['summary']['replacements'])
        self.assertFalse(report['candidate_results'])

    def test_route_or_feedback_change_abandons_stale_cycle(self):
        self.migrated()
        first, _ = self.step()
        self.deliver(first)
        (self.root/'config/home-route-feedback.json').write_text('{"good":{},"bad":{},"revision":2}')
        second, report = self.step()
        self.assertNotEqual(first['cycle_id'], second['cycle_id'])
        self.assertIsNone(report)
        self.assertFalse(self.state['cycle']['results'])

    def test_processed_observation_tampering_rejected(self):
        self.migrated()
        task, _ = self.step()
        self.deliver(task)
        self.step()
        path = self.reports/'observations'/self.probe/(task['batch_id']+'.json')
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.step()

    def test_stale_task_and_wrong_route_rejected(self):
        self.migrated()
        task, _ = self.step()
        with self.assertRaises(ValueError):
            validate_task(task, self.probe, epoch(task['expires_utc'])+1)
        value = self.deliver(task)
        value['results'][0]['result']['url'] = 'http://other.test/'
        with self.assertRaisesRegex(ValueError, 'route mismatch'):
            validate_observations(value, task, self.now)

    def test_durable_budget_survives_retry_and_refund_is_idempotent(self):
        self.migrated()
        task, _ = self.step()
        ledger, reason = reserve({}, task, self.config, self.now)
        self.assertFalse(reason)
        value = self.deliver(task)
        settled = settle(ledger, value)
        self.assertEqual(value['usage']['downloaded_bytes'], settled['bytes'])
        self.assertEqual(settled, settle(settled, value))
        interrupted = dict(value, stop_reason='interrupted_batch')
        self.assertEqual(ledger['bytes'], settle(ledger, interrupted)['bytes'])
        capped = dict(self.config, daily_bytes=task['limits']['bytes']-1)
        self.assertEqual('daily_byte_budget', reserve({}, task, capped, self.now)[1])


class TransportBoundsTests(unittest.TestCase):
    def test_sample_uses_small_memory_instead_of_retaining_video(self):
        class Response:
            headers = {'Content-Length': str(6*1024*1024)}
            status = 200
            remaining = 6*1024*1024
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def geturl(self): return 'http://fixture.test/segment.ts'
            def read1(self, amount):
                count = min(amount, self.remaining)
                self.remaining -= count
                return b'x' * count
            read = read1
        with mock.patch.object(home_probe, 'open_request', return_value=Response()):
            tracemalloc.start()
            sample = home_probe.segment_sample('http://fixture.test/segment.ts', 6, 6*1024*1024)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        self.assertEqual(6*1024*1024, sample['downloaded_bytes'])
        self.assertLess(peak, 512*1024)

    def test_transport_byte_limit_counts_all_upstream_reads(self):
        sock = mock.Mock()
        sock.recv.side_effect = lambda n: b'x'*n
        budget = DownloadBudget(100, time.monotonic()+20)
        self.assertEqual(80, len(budget.receive(sock,80)))
        self.assertEqual(20, len(budget.receive(sock,80)))
        with self.assertRaisesRegex(RuntimeError, 'byte_budget'):
            budget.receive(sock,1)
        self.assertEqual(100,budget.used)
        self.assertEqual(2,sock.recv.call_count)

    def test_api_upload_pins_report_branch_and_never_overwrites(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'{}')
        api = GithubData('secret-not-printed', opener)
        api.put_immutable('observations/home-ac86u-test/'+'a'*64+'.json', b'{}')
        req = opener.open.call_args.args[0]
        self.assertTrue(req.full_url.startswith(API+'/contents/observations/'))
        self.assertEqual('home-reports', json.loads(req.data)['branch'])
        self.assertNotIn('sha', json.loads(req.data))
        with self.assertRaises(ValueError):
            api.put_immutable('.github/workflows/evil.yml', b'{}')

    def test_lost_upload_reply_requires_identical_remote_bytes(self):
        api = GithubData('secret-not-printed')
        with mock.patch.object(api,'request',side_effect=OSError('lost')), mock.patch.object(api,'get',return_value=b'{}'):
            api.put_immutable('observations/test/a.json', b'{}')
        with mock.patch.object(api,'request',side_effect=OSError('lost')), mock.patch.object(api,'get',return_value=b'different'):
            with self.assertRaisesRegex(RuntimeError,'differs'):
                api.put_immutable('observations/test/a.json', b'{}')


class AdditionalFlowTests(ThinFixture):
    def test_discovery_budget_finishes_formal_report_and_keeps_candidate_queue(self):
        self.migrated()
        current, _ = self.step()
        self.deliver(current)
        candidate, _ = self.step()
        self.deliver(candidate, subset=0, stop='discovery_budget')
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertEqual(2, report['summary']['good'])
        self.assertEqual(1, len(self.state['queue']))
        self.assertFalse(self.state['tested'])

    def test_exact_viewer_acceptance_preserves_playing_current_route(self):
        self.migrated()
        feedback = {'good':{'cctv1':[{'url':'http://current.test/one.m3u8'}]},'bad':{}}
        (self.root/'config/home-route-feedback.json').write_bytes(encode(feedback))
        task, _ = self.step()
        current = next(t for t in task['tasks'] if t['channel_key']=='cctv1')
        self.assertTrue(current['viewer_accepted_quality'])
        raw = measured(current, 'GOOD')
        raw.update(height=720, stream_mbps=2, bitrate_mbps=2)
        self.assertEqual('GOOD', cloud.normalise(raw, current)['observed_status'])
        raw['min_download_mbps'] = 1
        self.assertEqual('DEGRADED', cloud.normalise(raw, current)['observed_status'])

    def test_afternoon_uses_unexpired_qualified_primary_backup_without_reprobe(self):
        self.migrated()
        task, _ = self.step()
        self.deliver(task)
        task, _ = self.step()
        self.deliver(task)
        self.step()
        self.now = epoch('2026-09-15T05:05:00Z')
        task, _ = self.step()
        self.deliver(task, {'cctv1':'UNAVAILABLE'})
        task, _ = self.step()
        self.assertTrue(all(t['role']=='current' for t in task['tasks']))
        self.deliver(task, {'cctv1':'UNAVAILABLE'})
        _, report = self.step()
        self.assertEqual(1, report['summary']['replacements'])
        self.assertEqual('primary-cache', report['candidate_results'][0]['purpose'])

    def test_locally_interrupted_candidate_is_left_unmeasured(self):
        from router.ac86u import thin_probe as worker, home_transport as ht
        from types import SimpleNamespace
        self.migrated()
        task, _ = self.step()
        self.deliver(task)
        task, _ = self.step()
        watch = SimpleNamespace(reason='', peak=20000, minimum=120000)
        watcher = mock.MagicMock()
        watcher.__enter__.return_value = watch
        transport = mock.MagicMock()
        transport.__enter__.return_value = transport
        def interrupted(*args, **kwargs):
            watch.reason = 'memory_reserve'
            return measured(task['tasks'][0])
        with mock.patch.object(worker, 'ResourceWatch', return_value=watcher), \
             mock.patch.object(ht, 'HomeTransport', return_value=transport), \
             mock.patch.object(worker.time, 'time', return_value=self.now), \
             mock.patch.object(home_probe, 'probe_route', side_effect=interrupted):
            value = worker.measure(task, self.config, {}, self.root)
        self.assertFalse(value['results'])
        self.assertEqual('memory_reserve', value['stop_reason'])
        self.assertIsNone(home_probe._active_transport)

    def test_trial_archive_requires_fresh_household_verification(self):
        candidate = self.manifest['candidates'][0]
        state = cloud.import_legacy(self.state, {'state.json': b'{}',
            'pipeline-trial/state.json': encode({'backup_archive':{candidate['candidate_id']:candidate}})}, 'sha')
        self.assertTrue(state['archive'][candidate['candidate_id']]['requires_home_reverification'])

    def test_trial_pool_keeps_later_formal_rechecks_and_requires_reverification(self):
        from router.ac86u.home_contract import TRIAL_BACKUP_SCHEMA, TRIAL_ROUTE_CONTEXT
        from router.ac86u.home_decision import probe_verification
        candidate = self.manifest['candidates'][0]
        identity = candidate['candidate_id']
        row = dict(candidate, qualification='QUALIFIED',
                   source_manifest_sha256='a'*64, qualified_utc=timestamp(self.now),
                   last_verified_utc=timestamp(self.now), expires_utc=timestamp(self.now+3600),
                   verification=probe_verification(measured(candidate)))
        pool = {'schema': TRIAL_BACKUP_SCHEMA, 'probe_id': self.probe,
                'route_context': TRIAL_ROUTE_CONTEXT, 'generated_utc': timestamp(self.now),
                'formal_playlist_sha256': hashlib.sha256(self.formal).hexdigest(),
                'candidate_manifest_sha256': 'a'*64, 'backup_count': 1, 'backups': [row]}
        archived = dict(row, last_recheck_utc=timestamp(self.now+1),
                        last_recheck_result={'observed_status': 'UNAVAILABLE'},
                        retry_after_epoch=self.now+61)
        archived['verification'] = dict(row['verification'], min_download_mbps=1.0)
        sources = {'state.json': encode({'backup_archive': {identity: archived}}),
                   'pipeline-trial/qualified-backups.json': encode(pool)}
        before = copy.deepcopy(sources)
        state = cloud.import_legacy(self.state, sources, 'sha')
        self.assertEqual(dict(archived, requires_home_reverification=True), state['archive'][identity])
        self.assertEqual(before, sources)
        self.assertFalse(self.state['archive'])
        self.assertIn(identity, state['tested'])

        # An accepted formal pool still takes precedence and clears trial gating.
        formal_pool = dict(pool, schema='iptv-home-qualified-backups/v1',
                           route_context='living-room-path-equivalent')
        sources['qualified-backups.json'] = encode(formal_pool)
        state = cloud.import_legacy(self.state, sources, 'sha')
        self.assertEqual(row, state['archive'][identity])
        self.assertNotIn('requires_home_reverification', state['archive'][identity])

    def test_migration_chunks_roundtrip_and_corruption_stops_acceptance(self):
        from scripts.upload_home_thin_history import prepare
        backup = self.root/'backup'
        backup.mkdir()
        original = encode({'tested_candidate_ids':['old-id'], 'candidate_observations':{}, 'candidate_queue':[]})
        (backup/'state.json').write_bytes(original)
        (backup/'quality-policy.json').write_bytes(encode({'minimum_height_default':1080}))
        manifest, chunks = prepare(backup)
        destination = self.reports/'migrations'/self.probe
        destination.mkdir(parents=True)
        (destination/'manifest.json').write_bytes(encode(manifest))
        for identity, raw in chunks.items():
            (destination/(identity+'.json')).write_bytes(raw)
        files, migration_id = cloud.read_migration(destination)
        self.assertEqual(original, files['state.json'])
        imported = cloud.import_legacy(self.state, files, migration_id)
        self.assertTrue(imported['migration_complete'])
        self.assertIn('old-id', imported['tested'])
        identity = next(iter(chunks))
        (destination/(identity+'.json')).write_bytes(b'{"data":"eA=="}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            cloud.read_migration(destination)

    def test_late_retry_cannot_replace_already_accepted_attempt(self):
        self.migrated()
        first, _ = self.step()
        second, _ = self.step(seconds=1801)
        self.assertEqual(first['tasks'], second['tasks'])
        self.deliver(second)
        self.step()
        # The older lease can arrive late with valid original measurement times.
        original_now = self.now
        self.now = epoch(first['created_utc']) + 2
        self.deliver(first, {'cctv1':'UNAVAILABLE'})
        self.now = original_now
        self.step()
        first_id = first['tasks'][0]['task_id']
        self.assertEqual('GOOD', self.state['cycle']['results'][first_id]['result']['observed_status'])


class ThinOperationalTests(unittest.TestCase):
    def test_resource_watch_stops_only_owned_ffprobe(self):
        from router.ac86u import thin_probe as worker
        config = {'minimum_available_kib':65536, 'maximum_probe_rss_kib':65536}
        with mock.patch.object(worker,'available_memory',return_value=60000), \
             mock.patch.object(worker,'process_memory',return_value=(20000,[12345])), \
             mock.patch.object(worker.os,'kill') as kill:
            watch = worker.ResourceWatch(config, time.monotonic()+30)
            watch.done.wait = lambda _: watch.done.set()
            watch.watch()
            self.assertEqual('memory_reserve', watch.reason)
            kill.assert_called_once_with(12345, worker.signal.SIGTERM)

    def test_dispatch_is_fixed_event_and_does_not_accept_payload_commands(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'')
        api = GithubData('secret-not-printed', opener)
        self.assertTrue(api.notify())
        req = opener.open.call_args.args[0]
        self.assertEqual('POST', req.method)
        self.assertEqual({'event_type':'home-observations-ready'}, json.loads(req.data))
        with self.assertRaises(ValueError):
            api.request('/dispatches', payload={'event_type':'execute-command'})

    def test_staging_and_rollback_preserve_history_and_other_services(self):
        from router.ac86u import thin_install as installer
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name:root/name.lower() for name in ('BASE','LEGACY_BASE','MANUAL_LOCK','ROOT',
                'DATA','CONFIG','THIN_CONFIG','SERVICES')}
            paths['BACKUP'] = paths['ROOT']/'before-thin-v1'
            paths['ROOT'].mkdir()
            paths['LEGACY_BASE'].mkdir()
            (paths['LEGACY_BASE']/'run.sh').write_text('#!/bin/sh\necho legacy\n')
            history = b'{"tested_candidate_ids":["preserve-me"]}'
            (paths['ROOT']/'state.json').write_bytes(history)
            paths['SERVICES'].write_text('#!/bin/sh\nother-service\n# BEGIN IPTV_HOME_PROBE\nold-cron\n# END IPTV_HOME_PROBE\n')
            config = {'probe_id':'home-ac86u-8f8908f0fba9', 'route_context':'living-room-path-equivalent',
                'runtime_transport':'merlinclash-marked', 'protected_publishing_ready':True,
                'daily_worker_enabled':True, 'ffprobe':shutil.which('ffprobe') or str(paths['LEGACY_BASE']/'run.sh')}
            original = encode(config)
            paths['CONFIG'].write_bytes(original)
            bundle = root/'bundle'
            bundle.mkdir()
            source = Path(__file__).resolve().parents[1]
            for name in installer.FILES:
                shutil.copyfile(source/'router/ac86u'/name, bundle/name)
            shutil.copyfile(source/'config/home-thin.json', bundle/'home-thin.json')
            jobs = {'IPTVHomePrimary':'0 2 * * * old-run', 'OtherService':'*/5 * * * * other'}
            def cron(*args):
                if args[0] == 'l':
                    return ''.join(command+' #'+name+'#\n' for name,command in jobs.items())
                if args[0] == 'd':
                    jobs.pop(args[1], None)
                else:
                    jobs[args[1]] = args[2]
                return ''
            with mock.patch.multiple(installer, **paths), mock.patch.object(installer, 'cron', side_effect=cron):
                installer.install(bundle)
                self.assertFalse(json.loads(paths['THIN_CONFIG'].read_bytes())['enabled'])
                self.assertNotIn('IPTVHomePrimary', jobs)
                self.assertIn('OtherService', jobs)
                self.assertTrue((paths['ROOT']/'thin-mode.locked').exists())
                self.assertEqual(history, (paths['ROOT']/'state.json').read_bytes())
                with paths['SERVICES'].open('a') as stream:
                    stream.write('new-unrelated-service\n')
                installer.rollback()
                self.assertEqual(original, paths['CONFIG'].read_bytes())
                self.assertEqual(history, (paths['ROOT']/'state.json').read_bytes())
                self.assertIn('new-unrelated-service', paths['SERVICES'].read_text())
                self.assertEqual('0 2 * * * old-run', jobs['IPTVHomePrimary'])
                self.assertFalse((paths['ROOT']/'thin-mode.locked').exists())

    def test_startup_block_restore_preserves_unrelated_changes(self):
        from router.ac86u.thin_install import split_services, THIN_BLOCK
        before, block, after = split_services('#!/bin/sh\n' + THIN_BLOCK + '\ncustom unrelated command\n')
        restored = before + '# BEGIN IPTV_HOME_PROBE\nold cron\n# END IPTV_HOME_PROBE' + after
        self.assertIn('custom unrelated command', restored)
        self.assertNotIn('IPTVHomeThin', restored)
        with self.assertRaises(ValueError):
            split_services('# BEGIN IPTV_HOME_PROBE\nincomplete')

if __name__ == '__main__':
    unittest.main()
