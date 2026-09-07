import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from router.ac86u.daily_worker import enqueue, next_job, advance
from router.ac86u.peak_policy import apply_peak_policy


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class DailyWorkerTests(unittest.TestCase):
    def test_next_day_does_not_discard_unfinished_job_and_rechecks_preempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            yesterday = epoch('2026-09-06T18:00:00')
            today = yesterday + 86400
            old = enqueue(root, 'primary-0200', yesterday)
            enqueue(root, 'primary-0200', today)
            enqueue(root, 'primary-0200', today + 60)
            self.assertEqual(2, len(list((root / 'daily-jobs').glob('*.json'))))
            self.assertEqual(old, next_job(root, today)[0])
            afternoon = enqueue(root, 'recheck-1300', today)
            self.assertEqual(afternoon, next_job(root, today)[0])
            peak = enqueue(root, 'peak-2000', today)
            self.assertEqual(afternoon, next_job(root, today)[0])
            self.assertEqual(peak, next_job(root, epoch('2026-09-08T12:00:00'))[0])

    def test_no_round_or_duration_cap_and_final_required(self):
        job = dict(id='old', kind='primary-0200', phase='candidates', batches=10000, created=0)
        report = dict(summary=dict(candidate_queue_remaining=123), policy=dict(candidate_manifest_state='accepted'))
        result, publish = advance(job, report, 100000000)
        self.assertEqual('PENDING', result['state'])
        self.assertFalse(publish)
        report['summary']['candidate_queue_remaining'] = 0
        result, publish = advance(result, report, 100000001)
        self.assertEqual('final', result['phase'])
        self.assertFalse(publish)
        report['policy']['batch_complete'] = False
        result, publish = advance(result, report, 100000002)
        self.assertNotEqual('COMPLETE', result['state'])
        report['policy']['batch_complete'] = True
        result, publish = advance(result, report, 100000003)
        self.assertTrue(publish)
        self.assertEqual('COMPLETE', result['state'])

    def test_manifest_error_is_retained_for_retry(self):
        job = dict(kind='primary-0200', phase='candidates', batches=1)
        report = dict(summary=dict(candidate_queue_remaining=0), policy=dict(candidate_manifest_state='rejected:stale'))
        result, publish = advance(job, report, 100)
        self.assertEqual('WAITING_MANIFEST', result['state'])
        self.assertFalse(publish)

    def test_peak_bad_survives_offpeak_good_but_is_url_specific(self):
        bad = dict(channel_key='cctv1', url='https://a.test/1', status='BAD', failure_confirmed=True)
        night = epoch('2026-09-07T12:00:00')
        _, failures = apply_peak_policy([bad], {}, run_kind='peak-2000', now_epoch=night)
        good = dict(bad, status='GOOD', failure_confirmed=False)
        rows, failures = apply_peak_policy([good], failures, run_kind='primary-0200', now_epoch=night+21600)
        self.assertEqual('BAD', rows[0]['status'])
        self.assertTrue(rows[0]['peak_failure_retained'])
        rows, failures = apply_peak_policy([dict(good, url='https://new.test/1')], failures,
            run_kind='primary-0200', now_epoch=night+21600)
        self.assertEqual('GOOD', rows[0]['status'])
        _, failures = apply_peak_policy([good], failures, run_kind='peak-2000', now_epoch=night+86400)
        self.assertEqual({}, failures)

    def test_circuit_and_outside_window_cannot_create_peak_failure(self):
        bad = dict(channel_key='cctv1', url='https://a.test/1', status='BAD', failure_confirmed=True)
        _, failed = apply_peak_policy([bad], {}, run_kind='peak-2000', now_epoch=epoch('2026-09-07T03:00:00'))
        self.assertEqual({}, failed)
        _, failed = apply_peak_policy([bad], {}, run_kind='peak-2000', now_epoch=epoch('2026-09-07T12:00:00'), circuit_open=True)
        self.assertEqual({}, failed)


if __name__ == '__main__':
    unittest.main()
