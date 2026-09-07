import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from router.ac86u.daily_worker import enqueue, next_job, advance, primary_window, defer_primary_jobs
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

    def test_publish_before_and_between_candidate_batches_without_dropping_queue(self):
        job = dict(id='old', kind='primary-0200', phase='final', batches=10000, created=0)
        report = dict(summary=dict(candidate_queue_remaining=123), policy=dict(batch_complete=True))
        result, publish = advance(job, report, 100)
        self.assertTrue(publish)
        self.assertEqual('candidates', result['phase'])
        self.assertEqual('PENDING', result['state'])
        report['policy']['candidate_manifest_state'] = 'accepted'
        result, publish = advance(result, report, 101)
        self.assertFalse(publish)
        self.assertEqual('final', result['phase'])
        result, publish = advance(result, report, 102)
        self.assertTrue(publish)
        self.assertEqual('candidates', result['phase'])
        report['summary']['candidate_queue_remaining'] = 0
        result, _ = advance(result, report, 103)
        result, publish = advance(result, report, 104)
        self.assertTrue(publish)
        self.assertEqual('COMPLETE', result['state'])

    def test_manifest_error_is_retained_for_retry(self):
        job = dict(kind='primary-0200', phase='candidates', batches=1)
        report = dict(summary=dict(candidate_queue_remaining=0), policy=dict(candidate_manifest_state='rejected:stale'))
        result, publish = advance(job, report, 100)
        self.assertEqual('WAITING_MANIFEST', result['state'])
        self.assertFalse(publish)

    def test_beijing_window_boundaries_and_next_day_resume(self):
        for stamp, expected in [('2026-09-06T17:59:59', False),
                                ('2026-09-06T18:00:00', True),
                                ('2026-09-06T23:59:59', True),
                                ('2026-09-07T00:00:00', False),
                                ('2026-09-07T17:59:59', False)]:
            self.assertEqual(expected, primary_window(epoch(stamp))[0], stamp)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            start=epoch('2026-09-06T18:00:00')
            path=enqueue(root,'primary-0200',start)
            job=json.loads(path.read_text())
            job.update(phase='candidates', batches=9)
            path.write_text(json.dumps(job))
            cutoff=epoch('2026-09-07T00:00:00')
            defer_primary_jobs(root,cutoff)
            saved=json.loads(path.read_text())
            self.assertEqual('WAITING_WINDOW',saved['state'])
            self.assertEqual('candidates',saved['phase'])
            self.assertEqual(9,saved['batches'])
            self.assertEqual(start+86400,saved['retry_after'])
            self.assertIsNone(next_job(root,cutoff+3600))
            self.assertEqual(path,next_job(root,start+86400)[0])
            afternoon=enqueue(root,'recheck-1300',cutoff+5*3600)
            self.assertEqual(afternoon,next_job(root,cutoff+5*3600)[0])

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
