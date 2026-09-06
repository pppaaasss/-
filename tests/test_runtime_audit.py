import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class RuntimeAuditTests(unittest.TestCase):
    def test_shell_collector_preserves_healthy_hashes_and_first_python_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opt = root / 'opt'
            (opt / 'bin').mkdir(parents=True)
            (opt / 'lib').mkdir()
            python = opt / 'bin/python3'
            python.write_text('#!/bin/sh\necho PYTHON_OK\nexit 0\n')
            python.chmod(0o755)
            package = opt / 'bin/opkg'
            package.write_text('#!/bin/sh\necho "libpython3 - test"\n')
            package.chmod(0o755)
            library = opt / 'lib/libpython3.13.so'
            library.write_text('healthy')
            log = root / 'pipeline.log'
            log.write_text('PIPELINE_START\n')
            script = root / 'audit.sh'
            original = Path('router/ac86u/runtime_audit.sh').read_text()
            script.write_text(original.replace('/opt', str(opt)).replace('/tmp/iptv-pipeline-trial.log', str(log)))
            data = root / 'data'
            def audit(mode):
                return subprocess.run(['/bin/sh', str(script), str(data), mode],
                    capture_output=True, text=True, timeout=10, env=dict(os.environ, LC_ALL='C'))
            healthy = audit('baseline')
            self.assertEqual(0, healthy.returncode)
            self.assertIn('HEALTHY_BASELINE_SAVED', healthy.stdout)
            directory = data / 'runtime-diagnostics'
            baseline = (directory / 'baseline.sha256').read_bytes()
            python.write_text('#!/bin/sh\necho "Fatal Python error" >&2\nexit 1\n')
            library.write_text('changed')
            log.write_text('Frozen object is invalid\n')
            failed = audit('failure')
            self.assertIn('RUNTIME_CHECK_FAILED', failed.stdout)
            self.assertIn('FAILED', failed.stdout)
            self.assertIn('Frozen object is invalid', failed.stdout)
            self.assertEqual(baseline, (directory / 'baseline.sha256').read_bytes())
            first = (directory / 'first-failure.txt').read_bytes()
            python.write_text('#!/bin/sh\necho PYTHON_OK\nexit 0\n')
            audit('check')
            self.assertEqual(first, (directory / 'first-failure.txt').read_bytes())
            self.assertEqual(baseline, (directory / 'baseline.sha256').read_bytes())


if __name__ == '__main__':
    unittest.main()
