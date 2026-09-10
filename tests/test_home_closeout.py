"""Behavior regressions from the 2026-09-08 audit; no external media requests."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from router.ac86u import home_probe as probe, daily_worker as worker
from router.ac86u import candidate_delivery as delivery
from router.ac86u.progress_journal import replay_progress
from router.ac86u.home_contract import make_candidate, object_sha256
from scripts.build_home_candidate_manifest import build_incremental_manifest, retain_delivery
from tests.test_home_candidate_incremental import full, row
from tests.test_ac86u_home_probe import NOW, measured, seed_backup_pool

FORMAL = b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/cctv1.m3u8\n'


def config(root, **changes):
    return dict(dict(output_dir=str(root), probe_id='home-ac86u-test', maximum_load1=10000,
        minimum_mem_available_kib=1, candidate_manifest_url='', playlist_url='https://repo.test/core',
        batch_cycle_id='cycle', batch_phase='final', actionable=True), **changes)


class CloseoutTests(unittest.TestCase):
    def test_missed_batch_replayed_until_received_even_after_disappearance(self):
        delta, index, _ = build_incremental_manifest(full([row()]), None, bootstrap_if_missing=False)
        first, index = retain_delivery(delta, index, None)
        delta, next_index, _ = build_incremental_manifest(full([]), index)
        second, next_index = retain_delivery(delta, next_index, index)
        self.assertEqual(first['candidates'], second['candidates'])
        third, _ = retain_delivery(delta, next_index, next_index,
            receipt={'received_batches': [first['delivery_batches'][0]['id']]})
        self.assertEqual([], third['candidates'])

    def test_late_delivery_reopens_complete_job_but_waits_outside_window(self):
        for hour, expected in [(4, 'PENDING'), (13, 'WAITING_WINDOW')]:
            epoch = worker.datetime(2026, 9, 8, hour, tzinfo=worker.ZONE).timestamp()
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                path=worker.enqueue(root, worker.KINDS[0], epoch)
                job=json.loads(path.read_text());job['state']='COMPLETE';probe.atomic_json(path,job)
                with mock.patch.object(worker.subprocess, 'run', return_value=
                    worker.subprocess.CompletedProcess([], 0, '{"pending": true}', '')) as child:
                    worker.poll_delivery(config(root),root,epoch)
                self.assertEqual('candidate_delivery.py', Path(child.call_args.args[0][-1]).name)
                self.assertEqual(str(root), json.loads(child.call_args.kwargs['input'])['root'])
                jobs=[json.loads(p.read_text()) for p in (root/'daily-jobs').glob('*.json')]
                self.assertEqual(1,sum(j['state']==expected for j in jobs))
                self.assertEqual(hour==4,worker.next_job(root,epoch) is not None)

    def test_new_bad_overrides_cached_good_without_reprobing_unrelated_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=config(tmp)
            with mock.patch.object(probe,'fetch_playlist',return_value=(FORMAL,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'probe_route',side_effect=lambda name,url,*,floor,**kw:measured(name,url,floor)):
                report,_=probe.run(cfg,now_epoch=NOW)
                self.assertEqual('KEEP',report['decisions'][0]['action'])
                cfg['home_feedback']={'bad':{'CCTV-1':['https://current.test/cctv1.m3u8']}}
                report,_=probe.run(cfg,now_epoch=NOW+60)
                self.assertEqual('BAD',report['current_results'][0]['status'])
                self.assertNotEqual('KEEP',report['decisions'][0]['action'])

    def test_unknown_backup_preserves_identity_and_old_time_with_retry_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);seed_backup_pool(root,FORMAL,[1])
            old=json.loads((root/'qualified-backups.json').read_text())['backups'][0]
            cfg=config(root)
            def observe(name,url,*,floor,**kw):
                return measured(name,url,floor,'DEGRADED' if 'current.test' in url else 'UNKNOWN')
            with mock.patch.object(probe,'fetch_playlist',return_value=(FORMAL,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'probe_route',side_effect=observe):
                _,state=probe.run(cfg,now_epoch=NOW+60)
            kept=state['backup_archive'][old['candidate_id']]
            self.assertEqual(old['last_verified_utc'],kept['last_verified_utc'])
            self.assertEqual('UNKNOWN',kept['last_recheck_result']['qualification'])
            self.assertEqual(0,state['qualified_backup_pool']['backup_count'])
            self.assertGreater(kept['retry_after_epoch'],NOW+60)

    def test_second_address_interrupt_keeps_first_and_retries_only_unfinished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config(root,batch_phase='candidates',candidate_manifest_url='https://repo.test/new')
            candidates=[make_candidate(dict(name='CCTV-1',url='https://candidate.test/'+str(n),sources=['test'])) for n in (1,2)]
            manifest=dict(formal_playlist=dict(sha256=hashlib.sha256(FORMAL).hexdigest(),channel_count=1),
                          candidates=candidates,candidate_count=2,generated_utc=probe.utc_text(NOW))
            urls=[]
            def observe(name,url,*,floor,**kw):
                urls.append(url)
                if len(urls)==2: raise InterruptedError('controlled interruption')
                return measured(name,url,floor)
            with mock.patch.object(probe,'fetch_playlist',return_value=(FORMAL,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'fetch_candidate_manifest',return_value=(manifest,b'new',cfg['candidate_manifest_url'])), \
                 mock.patch.object(probe,'probe_route',side_effect=observe):
                with self.assertRaises(InterruptedError): probe.run(cfg,now_epoch=NOW)
            state=replay_progress(json.loads((root/'state.json').read_text()),root)
            self.assertEqual(1,len(state['tested_candidate_ids']))
            self.assertEqual(1,len(state['candidate_queue']))
            self.assertFalse((root/'latest.json').exists())
            with mock.patch.object(probe,'fetch_playlist',return_value=(FORMAL,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'fetch_candidate_manifest',return_value=(manifest,b'new',cfg['candidate_manifest_url'])), \
                 mock.patch.object(probe,'probe_route',side_effect=lambda name,url,*,floor,**kw:measured(name,url,floor)) as calls:
                _,state=probe.run(cfg,now_epoch=NOW+60)
                self.assertEqual(1,calls.call_count)
                self.assertNotEqual(urls[0],calls.call_args.args[1])
                self.assertEqual(2,len(state['tested_candidate_ids']))

    def test_queue_write_failure_cannot_acknowledge_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config(root,candidate_manifest_url='https://repo.test/new')
            manifest=dict(formal_playlist=dict(sha256=hashlib.sha256(FORMAL).hexdigest(),channel_count=1),
                          candidates=[],candidate_count=0,generated_utc=probe.utc_text(NOW))
            with mock.patch.object(probe,'fetch_playlist',return_value=(FORMAL,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'fetch_candidate_manifest',return_value=(manifest,b'new',cfg['candidate_manifest_url'])), \
                 mock.patch.object(probe,'atomic_json',side_effect=OSError('disk full')):
                with self.assertRaises(OSError): delivery.receive(cfg,root,NOW)
            self.assertFalse((root/'candidate-receipts.json').exists())

    def test_receipt_upload_needs_no_health_report_and_round_trips(self):
        from tests.test_ac86u_github_push import report_remote, run_git, AC86UGitHubPushTests
        from router.ac86u.push_home_report import push
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);remote=report_remote(root)
            cfg=AC86UGitHubPushTests().config(root,enabled=True)
            state=root/'state';state.mkdir()
            receipt=dict(schema='iptv-home-delivery-receipt/v1',probe_id='home-ac86u-test',
                received_batches=['a'*64],meaning='queue_persisted_not_tested')
            probe.atomic_json(state/'candidate-receipts.json',receipt)
            self.assertTrue(push(cfg,receipts_only=True,remote_url_override=str(remote)))
            raw=run_git(['--git-dir',str(remote),'show','home-reports:receipts/home-ac86u-test.json']).stdout
            self.assertEqual(receipt,json.loads(raw))
            self.assertFalse((state/'latest.json').exists())
            self.assertEqual([],list((state/'pending-reports').glob('*.json')))

    def test_retention_preserves_pending_permanent_ids_and_required_reports(self):
        from router.ac86u.push_home_report import prune_reports
        from tests.test_ac86u_github_push import report
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jobs=root/'daily-jobs';jobs.mkdir()
            probe.atomic_json(jobs/'done.json',dict(state='COMPLETE',completed=NOW-20*86400))
            probe.atomic_json(jobs/'waiting.json',dict(state='WAITING_WINDOW',completed=NOW-20*86400))
            probe.atomic_json(root/'state.json',dict(tested_candidate_ids=['forever']))
            log=root/'log';log.write_bytes(b'x'*(2*1048576))
            with mock.patch.object(worker,'Path',side_effect=lambda p:log if p=='/opt/var/log/iptv-home-probe.log' else Path(p)):
                worker.maintain_runtime(root,NOW)
            self.assertFalse((jobs/'done.json').exists());self.assertTrue((jobs/'waiting.json').exists())
            self.assertEqual(['forever'],json.loads((root/'state.json').read_text())['tested_candidate_ids'])
            self.assertLessEqual(log.stat().st_size,1048576)
            self.assertLessEqual(log.with_suffix('.log.1').stat().st_size,1048576)
            inbox=root/'inbox/home-ac86u-test';inbox.mkdir(parents=True)
            for i in range(5):
                probe.atomic_json(inbox/(str(i)+'.json'),report(generated=probe.utc_text(NOW-(30-i)*86400)))
            receipt=dict(probe_id='home-ac86u-test',report_generated_utc=probe.utc_text(NOW-26*86400),report_file='4.json')
            removed=prune_reports(root,'home-ac86u-test',{'2.json'},receipt,NOW)
            self.assertEqual({'1.json','3.json'},set(removed))
            self.assertEqual({'0.json','2.json','4.json'},{p.name for p in inbox.glob('*.json')})

    def test_failed_atomic_replace_preserves_old_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';probe.atomic_json(path,{'saved':1})
            with mock.patch.object(probe.os,'replace',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):probe.atomic_json(path,{'saved':2})
            self.assertEqual({'saved':1},json.loads(path.read_text()))

    def test_torn_journal_tail_can_be_retried_then_replayed(self):
        from router.ac86u.progress_journal import append_progress
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            append_progress(root,dict(kind='candidate',identity='a',observation={'qualification':'UNKNOWN'},backup=None))
            with (root/'progress.jsonl').open('ab') as f:f.write(b'{"torn":')
            state=replay_progress({},root)
            append_progress(root,dict(kind='candidate',identity='b',observation={'qualification':'UNKNOWN'},backup=None))
            self.assertEqual(['a','b'],replay_progress(state,root)['tested_candidate_ids'])

    def test_conflicting_channel_labels_are_rejected_without_url_guessing(self):
        from router.ac86u.home_contract import ContractError, station_key
        from scripts.build_home_candidate_manifest import build_manifest
        with self.assertRaises(ContractError):
            make_candidate(dict(name='CCTV-5+',channel_key='cctv5',url='https://test.example/live',sources=['x']))
        for a,b in [('CCTV-5','CCTV-5+'),('CCTV-8','CCTV-8K'),('CCTV-4','CCTV-4K')]:
            self.assertNotEqual(station_key(a),station_key(b))
        formal=FORMAL+b'#EXTINF:-1,CCTV-2\nhttps://current.test/2\n'
        manifest,summary=build_manifest(discovery_rows=[dict(name=n,url='https://test.example/same',sources=['x']) for n in ('CCTV-1','CCTV-2')],
            formal_bytes=formal,formal_url='https://repo.test/core',source_revision='test',generated_utc=probe.utc_text(NOW))
        self.assertEqual(0,manifest['candidate_count'])
        self.assertEqual(2,summary['rejected']['identity_conflict'])

    def test_partial_report_cannot_enter_upload_queue(self):
        from router.ac86u.push_home_report import queue_report
        from tests.test_ac86u_github_push import report
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);value=report();value['policy']={'batch_complete':False}
            probe.atomic_json(root/'latest.json',value)
            with self.assertRaisesRegex(RuntimeError,'partial'):
                queue_report({'probe_id':'home-ac86u-test'},root,root/'latest.json')
            self.assertFalse((root/'pending-reports').exists())

    def test_different_subscription_url_never_inherits_core_good(self):
        from scripts.audit_home_coverage import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'tv-core.m3u').write_bytes(FORMAL)
            for name in ('tv.m3u','tv-easy.m3u','tv-all.m3u'):
                (root/name).write_bytes(FORMAL.replace(b'https://current.test/cctv1.m3u8',b'https://alternate.test/1'))
            result=audit(root,{'current_results':[{'channel_key':'cctv1','url':'https://current.test/cctv1.m3u8','status':'GOOD'}]})
            for row in result['playlists'].values():
                self.assertEqual('UNVERIFIED',row['differences'][0]['evidence_status'])

    def test_current_progress_survives_interrupt_and_keeps_measurement_time(self):
        formal=FORMAL+b'#EXTINF:-1,CCTV-2\nhttps://current.test/cctv2.m3u8\n'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config(root)
            urls=[]
            def observe(name,url,*,floor,**kw):
                urls.append(url)
                if len(urls)==2:raise InterruptedError('second current')
                return measured(name,url,floor)
            with mock.patch.object(probe,'fetch_playlist',return_value=(formal,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'probe_route',side_effect=observe):
                with self.assertRaises(InterruptedError):probe.run(cfg,now_epoch=NOW)
            with mock.patch.object(probe,'fetch_playlist',return_value=(formal,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'probe_route',side_effect=lambda name,url,*,floor,**kw:measured(name,url,floor)) as calls:
                report,_=probe.run(cfg,now_epoch=NOW+60)
                self.assertEqual(1,calls.call_count)
                self.assertEqual(NOW,report['current_results'][0]['observed_epoch'])

    def test_new_veto_does_not_invalidate_unrelated_good(self):
        formal=FORMAL+b'#EXTINF:-1,CCTV-2\nhttps://current.test/cctv2.m3u8\n'
        with tempfile.TemporaryDirectory() as tmp:
            cfg=config(tmp)
            with mock.patch.object(probe,'fetch_playlist',return_value=(formal,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'probe_route',side_effect=lambda name,url,*,floor,**kw:measured(name,url,floor)) as calls:
                probe.run(cfg,now_epoch=NOW);calls.reset_mock()
                cfg['home_feedback']={'bad':{'CCTV-1':['https://current.test/cctv1.m3u8']}}
                report,_=probe.run(cfg,now_epoch=NOW+60)
                self.assertTrue(all(c.args[1]=='https://current.test/cctv1.m3u8' for c in calls.call_args_list))
                self.assertEqual('KEEP',report['decisions'][1]['action'])

    def test_crash_between_queue_and_ack_retries_new_ack_over_old_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config(root,candidate_manifest_url='https://repo.test/new')
            candidate=make_candidate(dict(name='CCTV-1',url='https://candidate.test/new',sources=['test']))
            manifest=dict(formal_playlist=dict(sha256=hashlib.sha256(FORMAL).hexdigest(),channel_count=1),
                          candidates=[candidate],candidate_count=1,generated_utc=probe.utc_text(NOW))
            probe.atomic_json(root/'candidate-receipts.json',dict(probe_id=cfg['probe_id'],received_batches=['a'*64]))
            with mock.patch.object(probe,'fetch_playlist',return_value=(FORMAL,cfg['playlist_url'],.1)), \
                 mock.patch.object(probe,'fetch_candidate_manifest',return_value=(manifest,b'new',cfg['candidate_manifest_url'])):
                with mock.patch.object(delivery,'save_receipts',side_effect=InterruptedError('after durable queue')):
                    with self.assertRaises(InterruptedError):delivery.receive(cfg,root,NOW)
                self.assertEqual(1,len(json.loads((root/'state.json').read_text())['candidate_queue']))
                self.assertTrue(delivery.receive(cfg,root,NOW+60))
            self.assertEqual([object_sha256([candidate])],json.loads((root/'candidate-receipts.json').read_text())['received_batches'])
