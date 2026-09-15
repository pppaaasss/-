"""Daytime policy takes effect for both new and already-issued cloud tasks."""
from router.ac86u.thin_contract import epoch, slot
from scripts import home_thin_control as cloud
from tests.test_home_thin import ThinFixture


class NightScheduleTests(ThinFixture):
    def setUp(self):
        super().setUp()
        self.migrated()
        self.config['candidate_mode'] = 'drain_queue'
        self.config['candidate_run_kinds'] = ['primary-0200']

    def test_afternoon_and_evening_leave_candidates_for_next_morning(self):
        self.now = epoch('2026-09-15T05:05:00Z')
        task, _ = self.step()
        self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        task, _ = self.step()
        self.assertEqual({'current'}, {t['role'] for t in task['tasks']})
        self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertFalse(report['candidate_results'])
        self.assertEqual(1, len(self.state['queue']))
        self.now = epoch('2026-09-15T12:05:00Z')
        task, _ = self.step()
        self.deliver(task)
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertFalse(report['candidate_results'])
        self.assertEqual(1, len(self.state['queue']))
        self.now = epoch('2026-09-15T18:05:00Z')
        task, _ = self.step()
        self.deliver(task)
        task, _ = self.step()
        self.assertEqual({'candidate'}, {t['role'] for t in task['tasks']})
        self.deliver(task)
        self.step()
        self.assertFalse(self.state['queue'])

    def test_midday_policy_change_withdraws_unexecuted_candidate_batch(self):
        self.config['candidate_run_kinds'] = list(cloud.KINDS)
        self.now = epoch('2026-09-15T05:05:00Z')
        task, _ = self.step()
        self.deliver(task)
        candidate, _ = self.step()
        self.assertEqual('candidate', candidate['tasks'][0]['role'])
        self.config['candidate_run_kinds'] = ['primary-0200']
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertFalse(report['candidate_results'])
        self.assertIn(candidate['batch_id'], self.state['batches'])
        self.assertEqual(1, len(self.state['queue']))

    def test_primary_window_still_measures_candidates(self):
        task, _ = self.step()
        self.deliver(task)
        candidate, _ = self.step()
        self.assertEqual('candidate', candidate['tasks'][0]['role'])
        status = cloud.status_snapshot(self.state, candidate, self.config, self.now)
        self.assertEqual(['primary-0200'], status['candidate_run_kinds'])

    def test_primary_window_extends_until_11_beijing(self):
        for instant in ('2026-09-14T18:00:00Z', '2026-09-15T00:00:00Z',
                        '2026-09-15T02:59:59Z'):
            self.assertEqual(('primary-0200', '20260915', epoch('2026-09-15T03:00:00Z')),
                             slot(epoch(instant)))
        self.assertIsNone(slot(epoch('2026-09-15T03:00:00Z')))
        self.assertIsNone(slot(epoch('2026-09-14T17:59:59Z')))
        self.now = epoch('2026-09-15T02:30:00Z')
        task, _ = self.step()
        self.deliver(task)
        candidate, _ = self.step()
        self.assertEqual('candidate', candidate['tasks'][0]['role'])
        self.assertLessEqual(epoch(candidate['expires_utc']), epoch('2026-09-15T03:00:00Z'))
