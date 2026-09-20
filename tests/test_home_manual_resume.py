"""A one-time request resumes candidates without changing future schedules."""
import copy

from router.ac86u.thin_contract import epoch, validate_task
from scripts import home_thin_control as cloud
from tests.test_home_thin import ThinFixture


class ManualResumeTests(ThinFixture):
    def setUp(self):
        super().setUp()
        self.config['candidate_mode'] = 'drain_queue'
        self.config['candidate_run_kinds'] = ['primary-0200']
        self.config.pop('manual_candidate_resume', None)
        self.now = epoch('2026-09-15T07:05:00Z')
        self.migrated()

    def request(self):
        self.config['manual_candidate_resume'] = {
            'day': '20260915', 'run_kind': 'recheck-1300', 'extra_seconds': 10800}

    def test_completed_afternoon_reopens_once_without_repeating_current_routes(self):
        current, _ = self.step()
        self.deliver(current)
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertEqual(0, len(report['candidate_results']))
        charged = self.state['native_budget']['seconds']
        self.request()
        candidate, _ = self.step()
        self.assertEqual({'candidate'}, {r['role'] for r in candidate['tasks']})
        validate_task(candidate, self.probe, self.now)
        self.assertEqual('2026-09-15T07:35:06Z', candidate['expires_utc'])
        self.assertGreater(self.state['native_budget']['seconds'], charged)
        self.deliver(candidate)
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertEqual(1, len(report['candidate_results']))
        state = copy.deepcopy(self.state)
        idle, _ = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertEqual(state['cycle']['results'], self.state['cycle']['results'])
        self.assertEqual(1, len(self.state['manual_resumes']))
        self.assertEqual(1, len(self.state['tested']))

    def test_extra_time_resumes_budget_closed_cycle_without_resetting_usage(self):
        self.config['candidate_run_kinds'].append('recheck-1300')
        self.config['discovery_seconds'] = 1
        current, _ = self.step(); self.deliver(current)
        idle, _ = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertTrue(self.state['cycle']['optional_closed'])
        before = copy.deepcopy(self.state['native_budget'])
        self.request()
        candidate, _ = self.step()
        self.assertEqual('candidate', candidate['tasks'][0]['role'])
        self.assertFalse(self.state['cycle'].get('optional_closed'))
        self.assertEqual(before['seconds']+240, self.state['native_budget']['seconds'])
        self.assertEqual(before['bytes']+candidate['limits']['bytes'], self.state['native_budget']['bytes'])

    def test_window_end_and_next_day_restore_normal_candidate_schedule(self):
        self.request()
        current, _ = self.step(); self.deliver(current)
        candidate, _ = self.step()
        self.assertEqual('candidate', candidate['tasks'][0]['role'])
        self.now = epoch('2026-09-15T08:00:01Z')
        idle, _ = self.step()
        self.assertEqual('WAITING_WINDOW', idle['state'])
        self.assertEqual(1, len(self.state['queue']))
        for now in ('2026-09-15T12:05:00Z', '2026-09-16T05:05:00Z'):
            effective = cloud.resume_config(self.config, epoch(now))
            self.assertEqual(['primary-0200'], effective['candidate_run_kinds'])
            self.assertNotIn('_manual_resume', effective)
        evening = cloud.resume_config(self.config, epoch('2026-09-15T12:05:00Z'))
        tomorrow = cloud.resume_config(self.config, epoch('2026-09-16T05:05:00Z'))
        self.assertEqual(self.config['daily_seconds']+10800, evening['daily_seconds'])
        self.assertEqual(self.config['daily_seconds'], tomorrow['daily_seconds'])

    def test_status_reports_extra_allowance_and_byte_limit_still_applies(self):
        self.request()
        current, _ = self.step(); self.deliver(current)
        self.config['daily_bytes'] = self.state['native_budget']['bytes']
        idle, _ = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        status = cloud.status_snapshot(self.state, idle, self.config, self.now)
        self.assertEqual(self.config['daily_seconds']+10800, status['budget']['daily_seconds_limit'])
        self.assertIn('recheck-1300', status['candidate_run_kinds'])
        self.assertFalse(self.state['tested'])
        idle, _ = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])

    def test_active_task_keeps_its_identity_and_deadline(self):
        current, _ = self.step()
        self.request()
        pending, _ = self.step()
        self.assertEqual(current, pending)
        self.now = epoch('2026-09-15T07:58:00Z')
        # A fresh batch remains limited by the end of the chosen slot.
        self.state['cycle'].pop('outstanding')
        task, _ = self.step()
        self.assertEqual('2026-09-15T08:00:00Z', task['expires_utc'])
