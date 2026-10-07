import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.daily_home_intake import verified_text, github_file, build_daily, diverse_rows, admitted_daily_ids, target_day
from scripts import home_thin_control as cloud
from scripts.home_test_campaign import MANIFEST_PATH
from router.ac86u.home_contract import make_candidate
from router.ac86u.thin_contract import epoch, timestamp, encode
from tests.test_home_thin import ThinFixture


class SourceEvidenceTests(unittest.TestCase):
    def test_file_content_change_is_required_and_revision_pinned(self):
        now = epoch('2026-10-03T16:30:00Z')
        commits = [dict(sha='a'*40, commit={'committer':{'date':'2026-10-03T15:00:00Z'}}),
                   dict(sha='b'*40, commit={'committer':{'date':'2026-10-03T09:00:00Z'}})]
        seen=[]
        def read(url, api=False):
            seen.append(url)
            return json.dumps(commits).encode() if api else (b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://a.example.com/new' if 'a'*40 in url else b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://a.example.com/old')
        url='https://raw.githubusercontent.com/owner/repo/main/live.m3u'
        text, evidence=verified_text(url, now, read)
        self.assertIn('/new',text);self.assertTrue(evidence['priority_refresh'])
        self.assertIn('path=live.m3u',seen[0]);self.assertIn('a'*40,seen[1])
        with self.assertRaisesRegex(ValueError,'unchanged'):
            verified_text(url, now, lambda u,api=False:json.dumps(commits).encode() if api else b'same')
        with self.assertRaisesRegex(ValueError,'entries_unchanged'):
            verified_text(url, now, lambda u,api=False: json.dumps(commits).encode() if api else
                (b'#comment changed\n' if 'a'*40 in u else b'') +
                b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://a.example.com/same')
        with self.assertRaisesRegex(ValueError,'24h'):
            verified_text(url, now+86400,read)
        with self.assertRaises(ValueError):
            github_file('https://raw.githubusercontent.com/owner/repo/main/README.txt')

    def test_refresh_priority_never_defeats_host_diversity_or_cap(self):
        rows=[make_candidate(dict(name='CCTV-1',sources=['test'],url=f'https://h{i%10}.example.com/{i}')) for i in range(805)]
        chosen=diverse_rows(rows,priority_urls=[rows[0]['url']])
        self.assertEqual(len(chosen),500)
        from scripts.home_test_campaign import initial_host
        self.assertEqual(len({initial_host(r['url']) for r in chosen[:10]}),10)
        self.assertEqual(target_day(epoch('2026-10-03T16:30:00Z')),'20261004')


class DailyControllerTests(ThinFixture):
    def prepare_daily(self):
        self.migrated()
        self.config.update(daily_verified_intake=True,stop_temporary_campaigns=True,
            daily_candidates=500,candidate_mode='drain_queue',candidate_budget_mode='night_queue')
        self.manifest['candidates']=[make_candidate(dict(name='CCTV-1',sources=['verified'],url=f'https://h{i}.example.com/a')) for i in range(8)]
        from router.ac86u.home_contract import object_sha256
        self.manifest.update(candidate_count=8,candidate_set_sha256=object_sha256(self.manifest['candidates']),
            daily_intake_day='20260915',daily_target=500,source_evidence_sha256='a'*64)
        (self.root/'harvest/home-candidates.json').write_bytes(encode(self.manifest))

    def test_final_partial_batch_and_next_day_ledger(self):
        self.prepare_daily()
        task,_=self.step();self.deliver(task)
        self.state['native_budget']['candidates']=499
        task,_=self.step()
        self.assertEqual(len(task['tasks']),1)
        self.assertEqual(self.state['native_budget']['candidates'],500)
        self.deliver(task)
        idle,_=self.step()
        self.assertFalse(idle.get('tasks'))
        self.assertGreater(len(self.state['queue']),0)

    def test_fresh_intake_reopens_completed_slot_for_existing_planned_ids(self):
        self.prepare_daily()
        task,_=self.step();self.deliver(task)
        cloud.ingest(self.state,self.reports,self.now)
        cycle=self.state['cycle']
        slot=cycle['slot']
        ids=[r['candidate_id'] for r in self.manifest['candidates']]
        self.state['night_plan']={'day':'20260915','candidate_ids':ids}
        self.state['completed_slots'].append(slot)
        cycle.update(optional_closed=True,discovery_closed=True)
        first=self.manifest['candidates'][0]
        self.state['tested'][first['candidate_id']]={'result':first}
        self.state['native_budget']['candidates']=480
        budget=copy.deepcopy(self.state['native_budget'])
        tested=copy.deepcopy(self.state['tested'])
        task,_=self.step()
        self.assertTrue(task.get('tasks'))
        self.assertTrue(all(r['role']=='candidate' for r in task['tasks']))
        self.assertNotIn(first['candidate_id'],{r['candidate_id'] for r in task['tasks']})
        self.assertEqual(self.state['native_budget']['candidates'],484)
        self.assertEqual(self.state['native_budget']['bytes'],budget['bytes']+task['limits']['bytes'])
        self.assertEqual(self.state['tested'],tested)
        self.assertNotIn(slot,self.state['completed_slots'])
        # Once acknowledged, the same evidence cannot reopen an exhausted slot
        # repeatedly, even when unmeasured IDs remain in the plan.
        self.deliver(task)
        cloud.ingest(self.state,self.reports,self.now)
        self.state['completed_slots'].append(slot)
        self.state['native_budget']['candidates']=500
        before=copy.deepcopy(self.state['native_budget'])
        idle,_=self.step()
        self.assertEqual(idle['state'],'SLOT_COMPLETE')
        self.assertEqual(self.state['native_budget'],before)

    def test_stale_intake_never_imports_old_queue_or_temporary_results(self):
        self.prepare_daily()
        first=self.manifest['candidates'][0]
        self.state['temporary_campaigns']={'old':{'results':{first['candidate_id']:{'result':first}},'candidates':{first['candidate_id']:first}}}
        ids=admitted_daily_ids(self.manifest,self.state,self.formal,self.root/'config/home-route-feedback.json',self.now)
        self.assertNotIn(first['candidate_id'],ids)
        self.assertEqual(admitted_daily_ids(self.manifest,self.state,self.formal,self.root/'config/home-route-feedback.json',self.now+86400),[])

    def test_previous_day_full_budget_does_not_block_new_day_candidates(self):
        self.prepare_daily()
        task,_=self.step();self.deliver(task)
        self.state['native_budget'].update(day='20260914',candidates=500)
        task,_=self.step()
        self.assertTrue(task.get('tasks'))
        self.assertEqual(self.state['native_budget']['day'],'20260915')
        self.assertEqual(self.state['native_budget']['candidates'],4)

    def test_outside_primary_window_never_issues_daily_candidates(self):
        self.prepare_daily()
        self.now=epoch('2026-09-15T03:01:00Z')
        task,_=self.step();self.assertFalse(task.get('tasks'))
        self.now=epoch('2026-09-15T05:05:00Z')
        task,_=self.step()
        self.assertTrue(all(row['role']=='current' for row in task['tasks']))

    def test_progress_distinguishes_measured_from_reserved_and_accounts_overhead(self):
        self.prepare_daily()
        task,_=self.step();self.deliver(task)
        task,_=self.step()
        status=cloud.status_snapshot(self.state,task,self.config,self.now)['daily_candidates']
        self.assertEqual(status['measured'],0)
        self.assertEqual(status['candidate_batches'],1)
        self.assertGreater(status['formal_or_backup_batches'],0)
        self.assertIsNone(status['estimated_additional_window_capacity'])
        self.deliver(task);next_task,_=self.step()
        status=cloud.status_snapshot(self.state,next_task,self.config,self.now)['daily_candidates']
        self.assertEqual(status['measured'],4)
        self.assertIsNotNone(status['observed_batch_spacing_seconds'])
        self.assertFalse(status['estimate_is_guarantee'])

    def test_existing_800_manifest_uses_500_cap_without_resetting_ledger(self):
        self.prepare_daily()
        self.manifest['daily_target'] = 800
        (self.root/'harvest/home-candidates.json').write_bytes(encode(self.manifest))
        task,_=self.step();self.deliver(task)
        self.state['native_budget']['candidates'] = 499
        task,_=self.step()
        self.assertEqual(len(task['tasks']), 1)
        self.assertEqual(self.state['native_budget']['candidates'], 500)
        self.deliver(task)
        idle,_=self.step()
        self.assertFalse(idle.get('tasks'))
        self.assertEqual(self.state['native_budget']['candidates'], 500)

    def test_already_over_500_keeps_history_and_stops_new_candidates(self):
        self.prepare_daily()
        task,_=self.step();self.deliver(task)
        self.state['native_budget']['candidates'] = 528
        before=copy.deepcopy(self.state['tested'])
        idle,_=self.step()
        self.assertFalse(idle.get('tasks'))
        self.assertEqual(self.state['native_budget']['candidates'], 528)
        self.assertEqual(self.state['tested'], before)

    def test_lowered_candidate_cap_does_not_stop_afternoon_formal_checks(self):
        self.prepare_daily()
        task,_=self.step();self.deliver(task)
        self.state['native_budget']['candidates'] = 528
        self.now=epoch('2026-09-15T05:05:00Z')
        task,_=self.step()
        self.assertTrue(task.get('tasks'))
        self.assertTrue(all(row['role']=='current' for row in task['tasks']))
        self.assertEqual(self.state['native_budget']['candidates'], 528)

    def test_builder_history_veto_auth_and_shortfall(self):
        self.migrated()
        urls=['https://a.example.com/live','https://b.example.com/live?token=abc','https://c.example.com/live','https://d.example.com/live']
        rows=[dict(name='CCTV-1',url=u,sources=['verified']) for u in urls]
        self.state['tested'][make_candidate(rows[0])['candidate_id']]={'result':make_candidate(rows[0])}
        f=self.root/'config/home-route-feedback.json';f.write_text(json.dumps({'bad':{'cctv1':[{'url':urls[2]}]}}))
        m,summary=build_daily(rows,self.state,self.formal,f,self.now,[{'source':'verified','priority_refresh':True}])
        self.assertEqual([r['url'] for r in m['candidates']],[urls[3]])
        self.assertEqual(summary['shortfall'],499)
        self.assertFalse(m['production_eligible'])

    def test_large_discovery_filters_history_before_delivery_envelope_limit(self):
        self.migrated()
        rows=[dict(name='CCTV-1',url=f'https://pool.example.com/{i}',sources=['verified'])
              for i in range(10001)]
        for row in rows[:9500]:
            candidate=make_candidate(row)
            self.state['tested'][candidate['candidate_id']]={'result':candidate}
        before=copy.deepcopy(self.state['tested'])
        manifest,summary=build_daily(rows,self.state,self.formal,
            self.root/'config/home-route-feedback.json',self.now,[{'source':'verified'}])
        self.assertEqual(summary['discovery_rows'],10001)
        self.assertEqual(summary['history_and_safety_exclusions'],
                         {'formal_queue_archive_or_tested_history':9500})
        self.assertEqual(manifest['candidate_count'],500)
        self.assertEqual(summary['shortfall'],0)
        self.assertLessEqual(manifest['candidate_count'],500)
        self.assertEqual(self.state['tested'],before)
