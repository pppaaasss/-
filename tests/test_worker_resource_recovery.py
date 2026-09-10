import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from router.ac86u import daily_worker as worker


class ResourceRecoveryTests(unittest.TestCase):
    def test_resource_stop_preserves_phase_and_precedes_network_circuit(self):
        for phase in ('candidates', 'final'):
            job = dict(kind='primary-0200', phase=phase, batches=42)
            report = dict(resources=dict(stop_reason='memory_below_51200'),
                          summary=dict(circuit_breaker_open=True, candidate_queue_remaining=1000),
                          policy=dict(batch_complete=True))
            updated, publish = worker.advance(job, report, 100)
            self.assertEqual('WAITING_RESOURCES', updated['state'])
            self.assertEqual(phase, updated['phase'])
            self.assertEqual(400, updated['retry_after'])
            self.assertFalse(publish)
            self.assertNotIn('candidate_manifest_checked', updated)

    def test_supervisor_import_does_not_load_probe_or_delivery(self):
        result = subprocess.run([sys.executable, '-c',
            "import sys; from router.ac86u import daily_worker; "
            "assert 'router.ac86u.home_probe' not in sys.modules; "
            "assert 'router.ac86u.candidate_delivery' not in sys.modules"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_child_resource_exit_updates_public_status_and_keeps_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.json'
            config.write_text(json.dumps(dict(output_dir=temp, daily_worker_enabled=True)))
            path = root / 'daily-jobs' / 'job.json'
            path.parent.mkdir()
            job = dict(id='job', kind='primary-0200', phase='candidates', state='PENDING', batches=2)
            path.write_text(json.dumps(job))
            with patch.object(worker, 'poll_delivery'), patch.object(worker, 'maintain_runtime'), \
                 patch.object(worker, 'defer_primary_jobs'), \
                 patch.object(worker, 'next_job', side_effect=[(path, job), None]), \
                 patch.object(worker, 'primary_window', return_value=(True, 9999999999, 0)), \
                 patch.object(worker, 'resource_check', return_value=('', {})), \
                 patch.object(worker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 75)):
                self.assertEqual(0, worker.work(config))
            saved = json.loads(path.read_text())
            status = json.loads((root / 'daily-status.json').read_text())
            self.assertEqual('WAITING_RESOURCES', status['state'])
            self.assertEqual('candidates', saved['phase'])
            self.assertEqual(saved, status)
            self.assertFalse((root / 'daily-runtime-error.json').exists())

    def test_delivery_child_failure_does_not_start_media_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(worker.subprocess, 'run',
                              return_value=subprocess.CompletedProcess([], 1, '', 'timeout')):
                worker.poll_delivery({}, root, 1000)
            self.assertEqual('WAITING_MANIFEST', json.loads((root / 'delivery-status.json').read_text())['state'])
            self.assertFalse((root / 'daily-jobs').exists())
