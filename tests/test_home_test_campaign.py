import copy
import json
from unittest import mock

from router.ac86u.home_contract import make_candidate, candidate_id
from router.ac86u.thin_contract import encode, timestamp, epoch
from scripts import home_thin_control as cloud
from scripts.home_test_campaign import MANIFEST_PATH, SCHEMA
from tests.test_home_thin import ThinFixture


class TemporaryCampaignTests(ThinFixture):
    def setUp(self):
        super().setUp()
        self.migrated()
        self.config.update(candidate_mode='drain_queue',candidate_budget_mode='night_queue',
            candidate_run_kinds=['primary-0200'])
        self.pool=[make_candidate(dict(name='CCTV-1' if i%2==0 else 'CCTV-2',
            url=f'https://public.com/temporary/{i}',sources=['public-list'])) for i in range(24)]
        self.campaign=dict(schema=SCHEMA,campaign_id='one-time-test',probe_id=self.probe,
            created_utc=timestamp(self.now-60),not_before_utc=timestamp(self.now-30),
            expires_utc=timestamp(self.now+2*86400),runtime_public_guard_verified=True,
            formal_sha256=cloud.sha256_bytes(self.formal),
            feedback_sha256=cloud.sha256_bytes((self.root/'config/home-route-feedback.json').read_bytes()),
            candidates=self.pool)
        self.save()

    def save(self):
        (self.root/MANIFEST_PATH).write_bytes(encode(self.campaign))

    def isolated(self):
        return self.state['temporary_campaigns'][self.campaign['campaign_id']]

    def first_temporary(self,bad=False):
        for _ in range(10):
            task,report=self.step()
            if task.get('temporary_campaign_id'):
                return task,report
            self.assertTrue(task.get('tasks'),task)
            self.assertTrue(all(r['role'] in ('current','backup') for r in task['tasks']))
            self.deliver(task,{'cctv1':'UNAVAILABLE'} if bad else None)
        self.fail('no isolated task')

    def test_good_temporary_results_never_enter_production_even_bad_main(self):
        task,report=self.first_temporary(bad=True)
        self.assertEqual(report['summary']['replacements'],0)
        self.assertEqual(report['candidate_results'],[])
        formal=copy.deepcopy({k:self.state[k] for k in ('tested','archive','cycle','last_report')})
        self.assertNotEqual(task['cycle_id'],self.state['cycle']['id'])
        self.assertLessEqual(len(task['tasks']),4)
        self.deliver(task)
        self.step()
        self.assertEqual(formal,{k:self.state[k] for k in formal})
        self.assertEqual(len(self.isolated()['results']),4)
        self.assertEqual(self.isolated()['outcomes'],{'QUALIFIED':4})
        self.assertFalse((self.reports/'inbox').exists())
        for name in ('tv-core.m3u','tv.m3u','tv-all.m3u','tv-easy.m3u'):
            self.assertEqual((self.root/name).read_bytes(),self.formal)

    def test_entire_registered_pool_not_limited_to_sixteen_per_night(self):
        task,_=self.first_temporary()
        batches=0
        while task.get('temporary_campaign_id'):
            batches+=1
            self.deliver(task)
            task,_=self.step()
            self.assertLess(batches,10)
        self.assertEqual(batches,6)
        self.assertEqual(len(self.isolated()['results']),24)
        self.assertEqual(self.isolated()['status'],'COMPLETE')
        self.assertEqual(self.state['tested'],{})
        self.assertEqual(self.state['archive'],{})
        self.assertEqual(len(self.state['queue']),1)

    def test_unexpired_lease_is_reused_without_double_reservation(self):
        task,_=self.first_temporary()
        budget=copy.deepcopy(self.state['native_budget'])
        retry,report=self.step()
        self.assertEqual(retry,task)
        self.assertIsNone(report)
        self.assertEqual(budget,self.state['native_budget'])

    def test_shared_refund_and_result_collection_are_idempotent(self):
        task,_=self.first_temporary()
        # End the manifest after this lease so collection cannot reserve another.
        self.deliver(task)
        (self.root/MANIFEST_PATH).unlink()
        self.step()
        first=copy.deepcopy(self.state['native_budget'])
        results=copy.deepcopy(self.isolated()['results'])
        self.step()
        self.assertEqual(first,self.state['native_budget'])
        self.assertEqual(results,self.isolated()['results'])
        self.assertNotIn(task['batch_id'],self.state['native_budget']['reservations'])
        self.assertEqual(first['discovery_bytes'],1024*1024)

    def test_registration_excludes_history_queue_archive_veto_conflicts_and_unsafe(self):
        self.state['tested'][candidate_id('cctv8',self.pool[0]['url'])]={'legacy':True}
        self.state['queue'][self.pool[1]['candidate_id']]=self.pool[1]
        self.campaign['candidates'] += [make_candidate(dict(name='CCTV-8',url=self.pool[2]['url'],sources=['conflict'])),
            make_candidate(dict(name='CCTV-1',url='http://127.0.0.1/a',sources=['unsafe']))]
        self.state['archive'][self.pool[3]['candidate_id']]=self.pool[3]
        feedback=self.root/'config/home-route-feedback.json'
        feedback.write_text(json.dumps({'bad':{'CCTV1':[{'url':self.pool[4]['url']}]}}))
        self.campaign['feedback_sha256']=cloud.sha256_bytes(feedback.read_bytes());self.save()
        self.step()
        rows=self.isolated()['candidates']
        for i in range(5):
            self.assertNotIn(self.pool[i]['candidate_id'],rows)
        self.assertGreaterEqual(len(self.isolated()['excluded']),7)

    def test_guard_required_and_absent_manifest_preserves_original_behavior(self):
        self.campaign['runtime_public_guard_verified']=False;self.save()
        _,report=self.step()
        self.assertIsNone(report)
        self.assertFalse(self.state.get('temporary_campaigns'))
        (self.root/MANIFEST_PATH).unlink()
        expected=cloud.production_step(self.state,self.config,self.root,self.reports,self.now+1)
        actual=cloud.step(self.state,self.config,self.root,self.reports,self.now+1)
        self.assertEqual(actual,expected)

    def test_manifest_identity_immutable_and_binding_change_blocks_dispatch(self):
        task,_=self.first_temporary()
        self.campaign['expires_utc']=timestamp(self.now+3*86400);self.save()
        later,_=self.step()
        self.assertFalse(later.get('temporary_campaign_id'))
        self.assertIn('cannot change',self.state['temporary_campaign_error'])
        self.assertEqual(len(self.isolated()['batch_ids']),1)

    def test_same_id_cannot_switch_pool(self):
        self.first_temporary()
        self.campaign['candidates']=self.pool[:1];self.save()
        self.step()
        self.assertIn('cannot change',self.state['temporary_campaign_error'])

    def test_daytime_expiry_and_late_results_never_become_formal(self):
        task,_=self.first_temporary()
        self.deliver(task)
        self.now=epoch('2026-09-15T05:05:00Z')
        later,_=self.step()
        self.assertTrue(all(r['role']=='current' for r in later['tasks']))
        self.assertEqual(len(self.isolated()['results']),4)
        self.assertFalse(self.state['tested'])
        self.assertFalse(self.state['archive'])
        # Existing batches permanently retain their ownership after manifest removal.
        self.assertEqual(self.state['batches'][task['batch_id']]['temporary_campaign_id'],'one-time-test')

    def test_fixed_budget_and_future_formal_reserve_stop_without_discarding_pool(self):
        self.config['daily_bytes']=5*64*1024*1024
        first,_=self.step();self.deliver(first)
        # Disable refunds to model conservative complete reservation costs.
        path=self.reports/'observations'/self.probe/(first['batch_id']+'.json')
        value=json.loads(path.read_bytes());value['native_evidence']={'usage_complete':False};path.write_bytes(encode(value))
        idle,_=self.step()
        self.assertEqual(idle['state'],'SLOT_COMPLETE')
        self.assertEqual(self.isolated()['status'],'WAITING_SHARED_BUDGET_WITH_FORMAL_RESERVE')
        self.assertEqual(len(self.isolated()['candidates']),24)
        self.state['night_plan']={'day':'20260915','candidate_ids':['x']*100000}
        self.assertEqual(cloud.night_budget_config(self.state,self.config,self.now),self.config)

    def test_partial_results_are_isolated_and_unmeasured_routes_can_resume(self):
        task,_=self.first_temporary();self.deliver(task,subset=1,stop='memory_reserve')
        task2,_=self.step()
        self.assertEqual(len(self.isolated()['results']),1)
        self.assertFalse(set(self.isolated()['results']) & {r['candidate_id'] for r in task2['tasks']})
        self.assertEqual(self.state['archive'],{})

    def test_expired_manifest_resumes_regular_candidates(self):
        task,_=self.first_temporary()
        self.now=epoch(self.campaign['expires_utc'])+1
        self.step()
        self.assertEqual(self.isolated()['status'],'EXPIRED')
        self.assertNotIn('outstanding',self.isolated())

    def test_guard_and_manifest_cannot_claim_unbounded_duration(self):
        self.campaign['expires_utc']=timestamp(self.now+31*86400);self.save()
        self.step()
        self.assertIn('interval',self.state['temporary_campaign_error'])
        self.assertFalse(self.state.get('temporary_campaigns'))

    def test_existing_formal_backup_reverification_precedes_temporary_candidates(self):
        from router.ac86u.home_decision import update_backup_pool
        from tests.test_home_thin import measured
        backup=make_candidate(dict(name='CCTV-1',url='https://oldbackup.com/one',sources=['formal-history']))
        raw=measured(dict(url=backup['url'],channel_key=backup['channel_key']))
        raw['min_height']=1080
        pool=update_backup_pool(None,[(backup,raw)],probe_id=self.probe,now_epoch=self.now-3600,
            formal_playlist_sha256='0'*64,candidate_manifest_sha256='0'*64,current_urls={},ttl_hours=36)
        self.state['archive'][backup['candidate_id']]=pool['backups'][0]
        first,_=self.step();self.deliver(first,{'cctv1':'UNAVAILABLE'})
        second,_=self.step();self.deliver(second,{'cctv1':'UNAVAILABLE'})
        recheck,_=self.step()
        self.assertEqual(recheck['tasks'][0]['role'],'backup')
        self.assertNotIn('temporary_campaign_id',recheck)
        self.assertFalse(self.isolated()['batch_ids'])
        self.deliver(recheck)
        temporary,_=self.step()
        self.assertEqual(temporary['temporary_campaign_id'],'one-time-test')

    def test_existing_unexpired_production_candidate_lease_is_not_hijacked(self):
        (self.root/MANIFEST_PATH).unlink()
        first,_=self.step();self.deliver(first)
        normal,_=self.step()
        self.assertEqual(normal['tasks'][0]['role'],'candidate')
        self.save()
        same,_=self.step()
        self.assertEqual(same,normal)
        self.assertFalse(self.isolated()['batch_ids'])

    def test_temporary_marker_cannot_leak_even_if_production_cycle_is_corrupt(self):
        task,_=self.first_temporary();self.deliver(task)
        state=copy.deepcopy(self.state)
        state['cycle']['id']=task['cycle_id']
        cloud.ingest(state,self.reports,self.now)
        self.assertEqual(state['cycle']['results'],self.state['cycle']['results'])
        self.assertEqual(state['archive'],{})
        self.assertEqual(state['tested'],{})

    def test_new_campaign_cannot_retest_old_temporary_results_or_relabel_url(self):
        task,_=self.first_temporary();self.deliver(task)
        # Collect without scheduling another lease.
        self.now=epoch('2026-09-15T03:00:00Z')
        self.step()
        observed={r['candidate_id'] for r in task['tasks']}
        changed=copy.deepcopy(self.campaign)
        changed['campaign_id']='another-campaign'
        changed['candidates'] += [make_candidate(dict(name='CCTV-8',url=task['tasks'][0]['url'],sources=['relabel']))]
        self.campaign=changed;self.save();self.step()
        registered=self.isolated()
        self.assertFalse(observed & set(registered['candidates']))
        self.assertTrue(all(r['url'] != task['tasks'][0]['url'] for r in registered['candidates'].values()))
        self.assertEqual(len(self.state['temporary_campaigns']['one-time-test']['results']),4)

    def test_next_night_resumes_only_unmeasured_inputs_after_formal_checks(self):
        task,_=self.first_temporary();self.deliver(task)
        self.now=epoch('2026-09-15T03:00:00Z')
        idle,_=self.step()
        self.assertEqual(idle['state'],'WAITING_WINDOW')
        completed=set(self.isolated()['results'])
        self.now=epoch('2026-09-15T18:05:00Z')
        formal,_=self.step()
        self.assertTrue(all(r['role']=='current' for r in formal['tasks']))
        self.deliver(formal)
        next_task,_=self.step()
        self.assertEqual(next_task['temporary_campaign_id'],'one-time-test')
        self.assertFalse(completed & {r['candidate_id'] for r in next_task['tasks']})
        self.assertEqual(len(self.isolated()['candidates']),24)

    def test_previous_day_late_observation_cannot_refund_new_day_budget(self):
        old,_=self.first_temporary()
        self.deliver(old)
        path=self.reports/'observations'/self.probe/(old['batch_id']+'.json')
        delayed=path.read_bytes();path.unlink()
        self.now=epoch('2026-09-15T18:05:00Z')
        formal,_=self.step()
        self.assertTrue(all(r['role']=='current' for r in formal['tasks']))
        before=copy.deepcopy(self.state['native_budget'])
        self.assertNotIn(old['batch_id'],before['reservations'])
        path.write_bytes(delayed)
        again,_=self.step()
        self.assertEqual(again,formal)
        self.assertEqual(self.state['native_budget'],before)
        self.assertEqual(len(self.isolated()['results']),4)
        self.assertFalse(self.state['tested'])

    def test_expired_retry_and_late_old_results_first_wins_and_both_settle_once(self):
        first,_=self.first_temporary()
        self.deliver(first,{'cctv1':'UNKNOWN','cctv2':'UNKNOWN'})
        path=self.reports/'observations'/self.probe/(first['batch_id']+'.json')
        delayed=path.read_bytes();path.unlink()
        self.now=epoch(first['expires_utc'])+1
        second,_=self.step()
        self.assertNotEqual(first['batch_id'],second['batch_id'])
        self.assertEqual(first['tasks'],second['tasks'])
        self.deliver(second)
        # Remove runtime input after issuance: both observations still settle/export.
        (self.root/MANIFEST_PATH).unlink()
        self.step()
        first_wins=copy.deepcopy(self.isolated()['results'])
        self.assertTrue(all(r['qualification']=='QUALIFIED' for r in first_wins.values()))
        self.assertIn(first['batch_id'],self.state['native_budget']['reservations'])
        path.write_bytes(delayed);self.step()
        self.assertEqual(first_wins,self.isolated()['results'])
        self.assertNotIn(first['batch_id'],self.state['native_budget']['reservations'])
        budget=copy.deepcopy(self.state['native_budget']);self.step()
        self.assertEqual(self.state['native_budget'],budget)
        self.assertEqual(budget['discovery_bytes'],2*1024*1024)
