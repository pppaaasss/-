import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class AckRetryTests(unittest.TestCase):
    def test_retries_are_bounded_and_do_not_probe_or_upload(self):
        source = Path('router/ac86u/native/native_worker.sh').read_text()
        function = source.split('retry_cloud_ack() {', 1)[1].split('\n}\n', 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            script = ('set -eu\ndata="$TASK_TEST_DIR"\n'
                      'date() { echo "$test_time"; }\n'
                      'notify() { echo wake >> "$data/calls"; }\n'
                      'retry_cloud_ack() {' + function + '\n}\n'
                      'test_time=1000; retry_cloud_ack\n'
                      'test_time=1030; retry_cloud_ack\n'
                      'test_time=1300; retry_cloud_ack\n')
            subprocess.run(['sh', '-c', script], check=True,
                           env=dict(os.environ, TASK_TEST_DIR=folder))
            self.assertEqual(['wake', 'wake'], Path(folder, 'calls').read_text().splitlines())
        self.assertLess(source.index('retry_cloud_ack\n  status'),
                        source.index('if [ "$now" -lt "$created" ]'))
        self.assertNotIn('curl', function)
        self.assertNotIn('upload', function)
