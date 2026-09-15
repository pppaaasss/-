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


class NativePollingFilesystemTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name)/'iptv-native'
        source = Path(installer.__file__).parent/'iptv_native.c'
        subprocess.run(['cc', '-Os', '-o', str(cls.binary), str(source), '-ldl', '-lm'], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def test_cross_filesystem_install_partial_retry_and_rollback(self):
        import os
        import shutil
        if not Path('/dev/shm').is_dir() or os.stat('/dev/shm').st_dev == os.stat(tempfile.gettempdir()).st_dev:
            self.skipTest('requires a second filesystem for actual EXDEV reproduction')
        run = (Path(installer.__file__).parent/'native_run.sh').read_bytes()
        old_run = run.replace(b'07|08|09|10|13', b'07|13')
        original = ('#!/bin/sh\nVPN_START\n'+installer.PREVIOUS_BLOCK+'\nOTHER_SERVICE\n').encode()
        updated = original.replace(installer.PREVIOUS_BLOCK.encode(), installer.BLOCK.encode())
        old_cron = (installer.PREVIOUS_SCHEDULE+' #IPTVHomeNative#\n').encode()
        new_cron = (installer.SCHEDULE+' #IPTVHomeNative#\n').encode()
        for mode in ('success', 'retry_partial', 'cron_failure', 'write_failure'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root, \
                 tempfile.TemporaryDirectory(dir='/dev/shm') as stage:
                initial_run = run if mode == 'retry_partial' else old_run
                with mock.patch.object(installer, 'read', side_effect=[original, initial_run, updated, run]), \
                     mock.patch.object(installer, 'ssh', side_effect=[old_cron, b'', new_cron, b'']) as remote:
                    installer.poll30()
                with tarfile.open(fileobj=io.BytesIO(remote.call_args_list[1].args[1]), mode='r:gz') as archive:
                    for member in archive.getmembers():
                        Path(stage, member.name).write_bytes(archive.extractfile(member).read())
                root = Path(root)
                base, data, services = root/'opt/runtime', root/'opt/data', root/'jffs/services-start'
                for folder in (base, data, services.parent, root/'bin'):
                    folder.mkdir(parents=True, exist_ok=True)
                # Match the router's minimal command set: mktemp is absent.
                # touch is used only by the failure-injection stubs below.
                for name in ('cp', 'chmod', 'rm', 'mkdir', 'touch'):
                    (root/'bin'/name).symlink_to(shutil.which(name))
                self.assertIsNone(shutil.which('mktemp', path=str(root/'bin')))
                services.write_bytes(original)
                (base/'native_run.sh').write_bytes(initial_run)
                (data/'ENABLED').touch()
                # Reproduce the original failure with the real native rename.
                cross = Path(stage)/'cross-device'
                cross.write_bytes(b'new file')
                failed = subprocess.run([str(self.binary), 'commit', str(cross), str(services)], capture_output=True)
                self.assertNotEqual(0, failed.returncode)
                self.assertIn(b'checkpoint_rename', failed.stderr)
                self.assertEqual(original, services.read_bytes())
                script = Path(stage, 'poll30.sh').read_text()
                for old, new in ((installer.BASE, str(base)), (installer.DATA, str(data)), (installer.SERVICES, str(services))):
                    script = script.replace(old, new)
                Path(stage, 'poll30.sh').write_text(script)
                native = base/'iptv-native'
                native.write_text('#!/bin/sh\n'
                    'if [ "$1" = commit ] && [ "$3" = "$SERVICES_TARGET" ] && '
                    '[ "$FAIL_WRITE" = 1 ] && [ ! -f "$FAIL_MARK" ]; then\n'
                    '  touch "$FAIL_MARK"; exit 9\nfi\nexec "$REAL_NATIVE" "$@"\n')
                native.chmod(0o755)
                cru = root/'bin/cru'
                cru.write_text('#!/bin/sh\n'
                    'if [ "$FAIL_CRON" = 1 ] && [ ! -f "$FAIL_MARK" ]; then touch "$FAIL_MARK"; exit 9; fi\n'
                    'printf "%s\\n" "$3" > "$CRON_CAPTURE"\n')
                cru.chmod(0o755)
                capture = root/'cron'
                result = subprocess.run(['/bin/sh', str(Path(stage)/'poll30.sh'), stage], capture_output=True,
                    env=dict(os.environ, PATH=str(root/'bin'),
                        REAL_NATIVE=str(self.binary), SERVICES_TARGET=str(services),
                        FAIL_WRITE=str(int(mode == 'write_failure')), FAIL_CRON=str(int(mode == 'cron_failure')),
                        FAIL_MARK=str(root/'failed'), CRON_CAPTURE=str(capture)))
                restored = mode in ('cron_failure', 'write_failure')
                self.assertEqual(2 if restored else 0, result.returncode, result.stderr.decode())
                self.assertEqual(original if restored else updated, services.read_bytes())
                self.assertEqual(initial_run if restored else run, (base/'native_run.sh').read_bytes())
                expected_cron = installer.PREVIOUS_SCHEDULE if restored else installer.SCHEDULE
                self.assertEqual(expected_cron.replace(installer.BASE, str(base)), capture.read_text().strip())
                if restored:
                    self.assertIn(b'POLL30_RESTORED', result.stderr)
                self.assertEqual(original, (data/'before-poll30/services.before').read_bytes())
                self.assertFalse(list(root.rglob('*.poll30.*')))
