import io
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from router.ac86u.native import native_from_termux as installer


class NativePollingTests(TestCase):
    def test_poll30_preserves_other_startup_content_and_verifies_both_targets(self):
        original = ('#!/bin/sh\nVPN_START\n'+installer.PREVIOUS_BLOCK+'\nOTHER_SERVICE\n').encode()
        updated = original.replace(installer.PREVIOUS_BLOCK.encode(), installer.BLOCK.encode())
        run = (Path(installer.__file__).parent/'native_run.sh').read_bytes()
        old_run = run.replace(b'07|08|09|10|13', b'07|13')
        old_cron = (installer.PREVIOUS_SCHEDULE+' #IPTVHomeNative#\n').encode()
        new_cron = (installer.SCHEDULE+' #IPTVHomeNative#\n').encode()
        with mock.patch.object(installer, 'read', side_effect=[original, old_run, updated, run]), \
             mock.patch.object(installer, 'ssh', side_effect=[old_cron, b'', new_cron, b'']) as remote:
            installer.poll30()
        archive_bytes = remote.call_args_list[1].args[1]
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
            self.assertEqual(updated, archive.extractfile('services.after').read())
            self.assertEqual(original, archive.extractfile('services.before').read())
            self.assertEqual(run, archive.extractfile('run.after').read())
            self.assertEqual(old_run, archive.extractfile('run.before').read())
            script = archive.extractfile('poll30.sh').read()
        subprocess.run(['sh', '-n'], input=script, check=True)
        self.assertIn(installer.sha(original).encode(), script)
        self.assertIn(installer.sha(old_run).encode(), script)
        self.assertNotIn(b'iptables', script)
        self.assertNotIn(b'rm -f '+installer.DATA.encode()+b'/ENABLED', script)
        self.assertNotIn(b'github.curl', script)

    def test_foreign_startup_block_or_cron_is_not_overwritten(self):
        for block, cron in ((installer.START+'\nCUSTOM\n'+installer.END, b''),
                            (installer.PREVIOUS_BLOCK, b'custom #IPTVHomeNative#\n')):
            with self.subTest(block=block), \
                 mock.patch.object(installer, 'read', return_value=block.encode()), \
                 mock.patch.object(installer, 'ssh', return_value=cron) as remote:
                with self.assertRaises(ValueError):
                    installer.poll30()
                self.assertTrue(all(len(call.args) == 1 for call in remote.call_args_list))

    def test_cron_payload_invokes_existing_guard_twice_with_30_second_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)/'run.sh'
            calls = Path(folder)/'calls'
            run.write_text('#!/bin/sh\nprintf "tick\\n" >> "'+str(calls)+'"\n')
            payload = installer.SCHEDULE.split(' ', 5)[5].replace(installer.BASE+'/native_run.sh', str(run))
            # Observe the requested delay without spending wall time sleeping.
            shell = 'sleep() { [ "$1" = 30 ] || exit 2; }; '+payload+'; wait'
            subprocess.run(['sh', '-c', shell], check=True)
            self.assertEqual(['tick', 'tick'], calls.read_text().splitlines())

    def test_morning_wrapper_update_is_idempotent_and_rejects_unknown_files(self):
        run = (Path(installer.__file__).parent/'native_run.sh').read_bytes()
        self.assertEqual(run, installer.extend_morning_window(run))
        with self.assertRaises(ValueError):
            installer.extend_morning_window(run+b'custom command\n')
