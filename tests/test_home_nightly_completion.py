"""A fixed 02:00-11:00 sweep accounts resources and preserves useful decisions."""
import copy
import json

from router.ac86u.home_contract import candidate_id
from router.ac86u.thin_contract import encode, epoch, timestamp, validate_task
from scripts import home_thin_control as cloud
from scripts.publish_home_decisions import publish_latest
from tests.test_home_thin import ThinFixture, measured


class NightlyCompletionTests(ThinFixture):
    def setUp(self):
        super().setUp()
        self.config.update(candidate_mode='drain_queue', candidate_budget_mode='night_queue',
            candidate_run_kinds=['primary-0200'])
        self.config.pop('manual_candidate_resume', None)
        self.migrated()

    def add_queue(self, count):
        original = self.manifest['candidates'][0]
        for n in range(count):
            url = original['url'] if n == 0 else f'http://candidate.test/{n}.m3u8'
            row = dict(original, url=url, candidate_id=candidate_id('cctv1', url))
            self.state['queue'][row['candidate_id']] = row

    def test_875_candidates_finish_without_manual_budget_changes_and_publish_only_qualified(self):
        self.add_queue(875)
        original_config = copy.deepcopy(self.config)
        task, _ = self.step(); self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        task, _ = self.step(); self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        seen = set()
        qualified = None
        for _ in range(225):
            task, report = self.step()
            if report:
                break
            validate_task(task, self.probe, self.now)
            self.assertEqual({'candidate'}, {r['role'] for r in task['tasks']})
            self.assertLessEqual(task['limits']['bytes'], 64*1024*1024)
            self.assertLessEqual(task['limits']['seconds'], 240)
            value = self.deliver(task, {'cctv1': 'UNKNOWN'})
            if qualified is None:
                qualified = task['tasks'][0]
                value['results'][0]['result'] = measured(qualified)
            # Unknown byte counters keep every full batch reservation, but
            # completed envelope timestamps still prove the 20-second runtime.
            value['usage']['runtime_s'] = 20
            value['finished_utc'] = timestamp(epoch(value['started_utc']) + 20)
            value['native_evidence'] = {'usage_complete': False,
                'usage_bytes_complete': False, 'usage_runtime_complete': True}
            (self.reports/'observations'/self.probe/(task['batch_id']+'.json')).write_bytes(encode(value))
            self.now += 80  # Includes a realistic cloud/cron handoff per batch.
            ids = {r['candidate_id'] for r in task['tasks']}
            self.assertFalse(seen & ids)
            seen.update(ids)
        self.assertEqual(875, len(seen))
        self.assertLess(self.now, epoch('2026-09-15T03:00:00Z'))
        self.assertFalse(self.state['queue'])
        self.assertEqual(original_config, self.config)
        self.assertIn(qualified['candidate_id'], self.state['archive'])
        self.assertEqual(1, len(self.state['archive']))
        self.assertTrue(report['policy']['candidate_sweep_complete'])
        self.assertEqual(0, report['summary']['candidate_queue_remaining'])
        self.assertEqual(1, report['summary']['replacements'])
        raw = encode(report)
        inbox = self.reports/'inbox'/self.probe
        inbox.mkdir(parents=True)
        (inbox/cloud.report_filename(report, raw)).write_bytes(raw)
        result = publish_latest(root=self.root, config_path=self.root/'config/home-publisher.json',
            inbox=self.reports/'inbox', now_epoch=self.now, apply=True)
        self.assertEqual(1, result['replacement_count'])
        self.assertIn(qualified['url'].encode(), (self.root/'tv-core.m3u').read_bytes())
        self.assertIn(b'http://current.test/two.m3u8', (self.root/'tv-core.m3u').read_bytes())

    def test_deadline_publishes_measured_backups_without_claiming_all_candidates_done(self):
        self.add_queue(9)
        self.now = epoch('2026-09-15T02:50:00Z')
        task, _ = self.step(); self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        task, _ = self.step(); self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        task, _ = self.step(); self.deliver(task)
        pending, _ = self.step()
        self.assertEqual(5, len(self.state['queue']))
        self.now = epoch('2026-09-15T03:00:01Z')
        idle, report = self.step()
        self.assertEqual('WAITING_WINDOW', idle['state'])
        self.assertEqual(1, report['summary']['replacements'])
        self.assertEqual(5, report['summary']['candidate_queue_remaining'])
        self.assertFalse(report['policy']['candidate_sweep_complete'])
        self.assertEqual(4, len(self.state['archive']))
        self.assertIn(pending['batch_id'], self.state['batches'])
        before = encode(self.state['last_report'])
        self.step()
        self.assertEqual(before, encode(self.state['last_report']))
        self.now = epoch('2026-09-15T18:05:00Z')
        task, _ = self.step(); self.deliver(task)
        task, _ = self.step()
        self.assertEqual({'candidate'}, {r['role'] for r in task['tasks']})
        self.assertFalse({r['candidate_id'] for r in task['tasks']} & set(self.state['tested']))

    def test_delayed_controller_closes_previous_night_before_starting_afternoon(self):
        self.add_queue(9)
        task, _ = self.step(); self.deliver(task)
        task, _ = self.step(); self.deliver(task)
        self.step()
        self.now = epoch('2026-09-15T05:05:00Z')
        task, report = self.step()
        self.assertEqual('primary-0200', report['run_kind'])
        self.assertFalse(report['policy']['candidate_sweep_complete'])
        self.assertEqual({'current'}, {r['role'] for r in task['tasks']})
        self.assertEqual('recheck-1300', task['run_kind'])

    def test_budget_plan_is_durable_counts_new_ids_once_and_does_not_increase_time(self):
        self.add_queue(875)
        task, _ = self.step()
        original = copy.deepcopy(self.state['night_plan'])
        budget = copy.deepcopy(self.state['native_budget'])
        status = cloud.status_snapshot(self.state, task, self.config, self.now)
        self.assertGreater(status['budget']['discovery_bytes_limit'], 875*12*1024*1024)
        self.assertEqual(self.config['daily_seconds'], status['budget']['daily_seconds_limit'])
        self.step()
        self.assertEqual(original, self.state['night_plan'])
        self.assertEqual(budget, self.state['native_budget'])
        self.add_queue(876)
        self.step()
        self.assertEqual(876, len(self.state['night_plan']['candidate_ids']))
        self.now = epoch('2026-09-15T12:05:00Z')
        effective = cloud.night_budget_config(self.state, self.config, self.now)
        self.assertGreater(effective['daily_bytes'], self.config['daily_bytes'])
        self.assertEqual(['primary-0200'], effective['candidate_run_kinds'])
        self.now = epoch('2026-09-15T18:05:00Z')
        task, _ = self.step()
        self.assertEqual('20260916', self.state['night_plan']['day'])
        self.assertEqual('20260916', self.state['native_budget']['day'])

    def test_time_limit_and_incomplete_current_checks_never_become_false_success(self):
        self.add_queue(9)
        task, _ = self.step(); self.deliver(task, subset=1)
        self.step()
        self.now = epoch('2026-09-15T03:00:01Z')
        idle, report = self.step()
        self.assertIsNone(report)
        self.assertFalse(self.state['tested'])
        self.assertEqual('WAITING_WINDOW', idle['state'])
        self.now = epoch('2026-09-15T18:05:00Z')
        task, _ = self.step(); self.deliver(task)
        self.config['discovery_seconds'] = 1
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertFalse(report['policy']['candidate_sweep_complete'])
        self.assertEqual(9, report['summary']['candidate_queue_remaining'])

    def test_new_intake_before_11_reopens_once_without_retesting_current_or_old_candidates(self):
        task, _ = self.step(); self.deliver(task)
        task, _ = self.step(); self.deliver(task)
        idle, report = self.step()
        self.assertTrue(report['policy']['candidate_sweep_complete'])
        before = copy.deepcopy(self.state['cycle']['results'])
        self.add_queue(2)
        task, _ = self.step()
        self.assertEqual(1, len(task['tasks']))
        self.assertEqual('candidate', task['tasks'][0]['role'])
        self.assertNotIn(task['tasks'][0]['candidate_id'], self.state['tested'])
        self.deliver(task)
        idle, report = self.step()
        self.assertTrue(report['policy']['candidate_sweep_complete'])
        self.assertEqual(2, len(self.state['tested']))
        for identity, row in before.items():
            self.assertEqual(row, self.state['cycle']['results'][identity])
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertIsNone(report)
