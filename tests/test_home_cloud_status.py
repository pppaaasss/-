"""Progress must not turn expired windows or issued tasks into router success."""
from router.ac86u.thin_contract import epoch, encode
from scripts import home_thin_control as cloud
from tests.test_home_thin import ThinFixture


class CloudStatusTests(ThinFixture):
    def test_issued_task_without_results_does_not_claim_router_running(self):
        self.migrated()
        task, _ = self.step()
        before = encode(self.state)
        status = cloud.status_snapshot(self.state, task, self.config, self.now)
        self.assertIsNone(status['router_enabled'])
        self.assertIsNone(status['last_heartbeat'])
        self.assertEqual(0, status['progress']['current_first_pass_completed'])
        self.assertEqual(2, status['progress']['current_total'])
        self.assertFalse(status['progress']['report_complete'])
        self.assertEqual('cloud_issued_not_router_confirmed', status['issued_task']['meaning'])
        self.assertEqual(before, encode(self.state))

    def test_progress_distinguishes_first_pass_rechecks_and_complete_report(self):
        self.migrated()
        task, _ = self.step()
        self.deliver(task, {'cctv1': 'UNAVAILABLE'})
        second, _ = self.step()
        progress = cloud.status_snapshot(self.state, second, self.config, self.now)['progress']
        self.assertEqual(2, progress['current_first_pass_completed'])
        self.assertEqual(1, progress['current_rechecks_required'])
        self.assertEqual(0, progress['current_rechecks_completed'])
        self.assertFalse(progress['report_complete'])
        self.deliver(second)
        candidate, _ = self.step()
        self.deliver(candidate)
        task, report = self.step()
        self.assertIsNotNone(report)
        status = cloud.status_snapshot(self.state, task, self.config, self.now)
        self.assertTrue(status['progress']['report_complete'])
        self.assertEqual(1, status['progress']['candidate_checks_completed'])
        self.assertEqual(1, status['progress']['current_rechecks_completed'])
        self.assertEqual(report['generated_utc'], status['last_report_generated_utc'])

    def test_previous_window_progress_is_not_the_next_mornings_progress(self):
        self.now = epoch('2026-09-14T14:40:00Z')
        self.migrated()
        task, _ = self.step()
        self.deliver(task, subset=1)
        self.step()
        self.now = epoch('2026-09-14T15:30:00Z')
        task, _ = self.step()
        status = cloud.status_snapshot(self.state, task, self.config, self.now)
        self.assertEqual('WAITING_WINDOW', status['state'])
        self.assertFalse(status['window']['active'])
        self.assertEqual('2026-09-14T18:00:00Z', status['window']['next_starts_utc'])
        self.assertFalse(status['progress']['belongs_to_active_window'])
        self.assertEqual(1, status['progress']['current_first_pass_completed'])
        self.assertIsNone(status['issued_task'])
        self.assertEqual('used_plus_unsettled_reservations', status['budget']['meaning'])
