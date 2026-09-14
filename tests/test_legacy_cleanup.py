"""Exercise the real shell/tar/lock transaction against an isolated router tree."""
from contextlib import redirect_stdout, redirect_stderr
import fcntl
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from router.ac86u import cleanup_legacy_from_termux as cleanup
from router.ac86u.native import native_from_termux as installer

ROOT = Path(__file__).resolve().parents[1]


class LegacyCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name)/'native'
        subprocess.run(['cc', '-Os', '-Wno-deprecated-declarations', '-o', str(cls.binary),
                        str(ROOT/'router/ac86u/native/iptv_native.c'), '-ldl', '-lm'], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root/'opt/var/lib/iptv-home-probe'
        self.data = self.root/'opt/var/lib/iptv-home-native'
        self.phone = self.root/'phone'
        self.phone.mkdir(mode=0o700)
        for path, value in {
            'opt/var/lib/iptv-home-probe/thin-mode.locked': 'frozen\n',
            'opt/var/lib/iptv-home-probe/before-native-v1/staged': '',
            'opt/var/lib/iptv-home-probe/before-native-v1/config.json': 'old settings',
            'opt/var/lib/iptv-home-probe/state.json': '{"history":"retain in backup"}',
            'opt/var/lib/iptv-home-probe/pipeline-trial/damaged.json': 'bad data',
            'opt/var/lib/iptv-home-probe/.old-cache': 'old cache',
            'opt/share/iptv-home-probe/run.sh': 'old runtime',
            'opt/etc/iptv-home-probe.json': 'old config',
            'opt/etc/iptv-home-probe/id_ed25519': 'synthetic old key',
            'opt/var/log/iptv-home-probe.log': 'old log',
            'opt/var/log/iptv-home-probe.log.1': 'rotated log',
            'opt/var/lib/iptv-home-native/status.txt': '2026-09-14T14:54:41Z\tUPLOADED\n',
            'opt/var/lib/iptv-home-native/resources.txt': 'COMPLETED\t11404\t112028\t34.073\n',
            'opt/var/lib/iptv-home-native/github.curl': 'synthetic new key',
            'jffs/scripts/services-start': '# native startup plus VPN startup\n',
            'opt/share/unrelated/keep': 'unrelated package',
            'opt/bin/cru': '#!/bin/sh\ncat "'+str(self.root/'jobs')+'"\n',
            'opt/bin/iptables': '#!/bin/sh\nprintf "%s\\n" "-N merlinclash"\n',
            'opt/bin/ssh': '#!/bin/sh\nexec /bin/sh -c "$2"\n',
            'jobs': '',
        }.items():
            target = self.root/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
            if path.startswith('opt/bin/'):
                target.chmod(0o700)
        runtime = self.root/'opt/share/iptv-home-native'
        runtime.mkdir()
        shutil.copy(self.binary, runtime/'iptv-native')
        self.module = SimpleNamespace(SSH_OPTIONS=[], HOST='fixture', remote_error=installer.remote_error)
        original_command = cleanup.remote_command
        self.command = lambda marker: (original_command(marker)
            .replace('/opt/', str(self.root)+'/opt/')
            .replace('/jffs/', str(self.root)+'/jffs/')
            .replace('cd /\n', 'cd '+str(self.root)+'\n'))
        patch = mock.patch.dict(os.environ, {'PATH':str(self.root/'opt/bin')+':'+os.environ['PATH']})
        patch.start()
        self.addCleanup(patch.stop)

    def run_cleanup(self):
        with mock.patch.object(cleanup, 'remote_command', side_effect=self.command):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cleanup.cleanup(self.module, self.phone)

    def assert_old_present(self):
        self.assertTrue((self.old/'state.json').exists())
        self.assertTrue((self.root/'opt/share/iptv-home-probe/run.sh').exists())
        self.assertTrue((self.root/'opt/etc/iptv-home-probe/id_ed25519').exists())

    def test_success_backs_up_bytes_holds_locks_and_preserves_new_runtime(self):
        original_verify = cleanup.verify_backup
        payload = b'large legacy observation\0' * 100003
        (self.old/'large-observation.bin').write_bytes(payload)
        inodes = {}
        def verify(path):
            for file in (self.data/'worker.lock', self.old/'daily-worker.lock',
                         self.old/'background-upgrade.lock'):
                inodes[file] = file.stat().st_ino
                with file.open('rb') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original_verify(path)
        with mock.patch.object(cleanup, 'verify_backup', side_effect=verify):
            self.run_cleanup()
        backup = self.phone/'legacy.tar'
        import tarfile
        with tarfile.open(backup) as archive:
            self.assertEqual(archive.extractfile('opt/etc/iptv-home-probe/id_ed25519').read(),
                             b'synthetic old key')
            self.assertEqual(archive.extractfile('opt/var/lib/iptv-home-probe/state.json').read(),
                             b'{"history":"retain in backup"}')
            self.assertEqual(archive.extractfile('opt/var/lib/iptv-home-probe/large-observation.bin').read(), payload)
        self.assertFalse((self.old/'state.json').exists())
        self.assertFalse((self.old/'.old-cache').exists())
        self.assertFalse((self.root/'opt/share/iptv-home-probe').exists())
        self.assertFalse((self.root/'opt/etc/iptv-home-probe').exists())
        self.assertFalse((self.root/'opt/var/log/iptv-home-probe.log.1').exists())
        for file, inode in inodes.items():
            self.assertEqual(file.stat().st_ino, inode)
        for relative in cleanup.REQUIRED:
            self.assertTrue((self.root/relative).is_file())
        self.assertEqual((self.data/'github.curl').read_text(), 'synthetic new key')
        self.assertEqual((self.root/'jffs/scripts/services-start').read_text(),
                         '# native startup plus VPN startup\n')
        self.assertTrue((self.root/'opt/share/unrelated/keep').exists())
        self.assertFalse((self.data/'ENABLED').exists())
        self.assertTrue((self.phone/'SHA256SUMS').read_text().startswith(original_verify(backup)))

    def test_enabled_pending_or_old_cron_prevents_deletion(self):
        for flag in ('ENABLED', 'outbox.native', 'PAUSED'):
            with self.subTest(flag=flag):
                target = self.data/flag
                target.touch()
                with self.assertRaises(RuntimeError):
                    self.run_cleanup()
                self.assert_old_present()
                target.unlink()
                (self.phone/'legacy.tar.part').unlink(missing_ok=True)
        (self.root/'jobs').write_text('* * * * * old-worker #IPTVHomeProbe#\n')
        with self.assertRaises(RuntimeError):
            self.run_cleanup()
        self.assert_old_present()

    def test_truncated_phone_backup_never_sends_delete_ack(self):
        original_verify = cleanup.verify_backup
        def truncate(path):
            with path.open('r+b') as file:
                file.truncate(1024)
            return original_verify(path)
        with mock.patch.object(cleanup, 'verify_backup', side_effect=truncate):
            with self.assertRaises((RuntimeError, cleanup.tarfile.TarError)):
                self.run_cleanup()
        self.assert_old_present()

    def test_phone_disk_sync_failure_keeps_old_files(self):
        with mock.patch.object(cleanup.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_cleanup()
        self.assert_old_present()

    def test_activation_during_phone_verification_aborts_cleanup(self):
        original_verify = cleanup.verify_backup
        def activate(path):
            digest = original_verify(path)
            (self.data/'ENABLED').touch()
            return digest
        with mock.patch.object(cleanup, 'verify_backup', side_effect=activate):
            with self.assertRaises(RuntimeError):
                self.run_cleanup()
        self.assert_old_present()
        self.assertTrue((self.phone/'legacy.tar').is_file())

    def test_no_ack_and_competing_native_lock_preserve_old_files(self):
        marker = 'fixture_done'
        result = subprocess.run(['/bin/sh', '-c', self.command(marker)],
                                input=b'', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_present()
        with (self.old/'daily-worker.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):
                self.run_cleanup()
        self.assert_old_present()


if __name__ == '__main__':
    unittest.main()
