import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from router.ac86u.pipeline_auto import next_action, run_batches, recovery_check
from router.ac86u import home_resources


def report(remaining, **summary):
    return dict(summary=dict(candidate_queue_remaining=remaining, circuit_breaker_open=False, **summary),
                resources={}, policy=dict(candidate_manifest_state='accepted', final_review_complete=True))


class AutoPipelineTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch('router.ac86u.pipeline_auto.recovery_check', return_value=('', {}))
        patch.start()
        self.addCleanup(patch.stop)

    def test_user_memory_threshold_and_recovery_margin(self):
        ticks = [[0, 0, 0, 100, 0, 0, 0, 0], [1, 0, 0, 199, 0, 0, 0, 0]]
        for available, expected in [(51199, 'memory_below_51200'), (51200, ''), (62000, '')]:
            with mock.patch.object(home_resources, 'cpu_ticks', side_effect=ticks), mock.patch.object(home_resources.time, 'sleep'):
                reason, _ = home_resources.sample_resources({}, lambda: dict(mem_available_kib=available))
                self.assertEqual(expected, reason)
        with mock.patch.object(home_resources, 'sample_resources', return_value=('', {})) as sample:
            recovery_check(dict(minimum_mem_available_kib=51200))
            self.assertEqual(59392, sample.call_args.args[0]['minimum_mem_available_kib'])
        root, config = self.fixture()
        config.write_text(json.dumps(dict(output_dir=str(root), minimum_mem_available_kib=65536)))
        def check(settings):
            self.assertEqual(51200, settings['minimum_mem_available_kib'])
            (root / 'pipeline-trial/auto-stop').touch()
            return 'waiting', {}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, run_batches(config, check_resources=check, sleep=lambda _: None))

    def test_empty_queue_only_completes_with_valid_manifest_and_no_stop(self):
        self.assertEqual('COMPLETE', next_action(report(0), None, 0)[0])
        for field, value, expected in (
            ('policy', dict(candidate_manifest_state='rejected:expired'), 'STOPPED_MANIFEST'),
            ('resources', dict(stop_reason='low_memory'), 'WAITING_RESOURCES'),
            ('summary', dict(candidate_queue_remaining=0, circuit_breaker_open=True), 'STOPPED_NETWORK'),
        ):
            row = report(0)
            row[field] = value
            self.assertEqual(expected, next_action(row, None, 0)[0])

    def test_repeated_no_progress_stops_but_progress_resets_counter(self):
        self.assertEqual(('STOPPED_NO_PROGRESS', 40, 3), next_action(report(40), 40, 2))
        self.assertEqual(('CONTINUING', 39, 0), next_action(report(39), 40, 2))

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / 'config.json'
        config.write_text(json.dumps(dict(output_dir=str(root))))
        return root, config

    def test_waits_for_manual_batch_then_continues_to_completion(self):
        root, config = self.fixture()
        results = iter([None, 2579, 2500, 0, 0])
        calls, sleeps = [], []

        def runner(args):
            calls.append(args)
            remaining = next(results)
            if remaining is None:
                return SimpleNamespace(returncode=1)
            (root / 'pipeline-trial/latest.json').write_text(json.dumps(report(remaining)))
            return SimpleNamespace(returncode=0)

        before = config.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, run_batches(config, runner=runner, sleep=sleeps.append))
        status = json.loads((root / 'pipeline-trial/auto-status.json').read_text())
        self.assertEqual('COMPLETE', status['state'])
        self.assertEqual(4, status['rounds'])
        self.assertEqual(5, len(calls))
        self.assertEqual(["candidates"] * 4 + ["final"], [c[-1] for c in calls])
        self.assertEqual([30, 30, 30, 30], sleeps)
        self.assertEqual(before, config.read_bytes())

    def test_stop_request_finishes_current_batch_without_starting_another(self):
        root, config = self.fixture()

        def runner(args):
            (root / 'pipeline-trial/latest.json').write_text(json.dumps(report(10)))
            (root / 'pipeline-trial/auto-stop').touch()
            return SimpleNamespace(returncode=0)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, run_batches(config, runner=runner, sleep=lambda _: None))
        status = json.loads((root / 'pipeline-trial/auto-status.json').read_text())
        self.assertEqual('STOPPED_BY_USER', status['state'])
        self.assertEqual(1, status['rounds'])

    def test_child_failure_does_not_read_old_success_report_or_restart(self):
        root, config = self.fixture()
        (root / 'pipeline-trial').mkdir()
        (root / 'pipeline-trial/latest.json').write_text(json.dumps(report(0)))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, run_batches(config, runner=lambda _: SimpleNamespace(returncode=2)))
        self.assertEqual('STOPPED_BATCH_ERROR', json.loads(
            (root / 'pipeline-trial/auto-status.json').read_text())['state'])

    def test_resource_wait_recovery_mid_batch_stop_and_start_race_continue(self):
        root, config = self.fixture()
        checks = iter([('low_memory', {}), ('', {}), ('', {}), ('low_memory', {}), ('', {}), ('', {})])
        child_results = iter([75, 'partial', 'complete', 'complete'])
        sleeps = []

        def runner(args):
            step = next(child_results)
            if step == 75:
                return SimpleNamespace(returncode=75)
            row = report(10 if step == 'partial' else 0)
            if step == 'partial':
                row['resources']['stop_reason'] = 'memory_below_65536'
            (root / 'pipeline-trial/latest.json').write_text(json.dumps(row))
            return SimpleNamespace(returncode=0)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, run_batches(config, runner=runner, sleep=sleeps.append,
                                            check_resources=lambda _: next(checks)))
        self.assertEqual([60, 60, 60, 60, 30], sleeps)
        status = json.loads((root / 'pipeline-trial/auto-status.json').read_text())
        self.assertEqual('COMPLETE', status['state'])
        self.assertEqual(3, status['rounds'])

    def test_final_review_must_finish_before_controller_claims_completion(self):
        root, config = self.fixture()
        phases = []
        def runner(args):
            phases.append(args[-1])
            row = report(0)
            row['policy']['final_review_complete'] = False
            (root / 'pipeline-trial/latest.json').write_text(json.dumps(row))
            return SimpleNamespace(returncode=0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, run_batches(config, runner=runner, sleep=lambda _: None))
        self.assertEqual(['candidates', 'final', 'final', 'final'], phases)
        self.assertEqual('STOPPED_FINAL_INCOMPLETE', json.loads(
            (root / 'pipeline-trial/auto-status.json').read_text())['state'])

    def test_upgrade_waits_for_old_controller_to_release_lock(self):
        import fcntl
        root, config = self.fixture()
        directory = root / 'pipeline-trial'
        directory.mkdir()
        phases, sleeps = [], []
        with (directory / 'auto.lock').open('a') as old:
            fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def sleep(seconds):
                sleeps.append(seconds)
                if seconds == 5:
                    self.assertTrue((directory / 'auto-stop').exists())
                    fcntl.flock(old, fcntl.LOCK_UN)
            def runner(args):
                self.assertFalse((directory / 'auto-stop').exists())
                phases.append(args[-1])
                (directory / 'latest.json').write_text(json.dumps(report(0)))
                return SimpleNamespace(returncode=0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, run_batches(config, runner=runner, sleep=sleep, replace_running=True))
        self.assertEqual(['candidates', 'final'], phases)
        self.assertEqual([5, 30], sleeps)

    def test_user_can_stop_while_resources_remain_low(self):
        root, config = self.fixture()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, run_batches(config,
                runner=lambda _: self.fail('must not launch when memory is low'),
                check_resources=lambda _: ('low_memory', {}),
                sleep=lambda _: (root / 'pipeline-trial/auto-stop').touch()))
        self.assertEqual('STOPPED_BY_USER', json.loads(
            (root / 'pipeline-trial/auto-status.json').read_text())['state'])


if __name__ == '__main__':
    unittest.main()
