import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from router.ac86u.native import repair_from_termux as repair

ROOT = Path(__file__).resolve().parents[1]


class RepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name)/'native'
        subprocess.run(['cc', '-Os', '-Wno-deprecated-declarations', '-o', str(cls.binary),
                        str(ROOT/'router/ac86u/native/iptv_native.c'), '-ldl', '-lm'], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def test_real_install_and_write_failure_restore_under_lock(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base, data, work, bins, legacy = [root/x for x in ('base', 'data', 'work', 'bin', 'legacy')]
                for folder in (base, data, work, bins, legacy):
                    folder.mkdir()
                original = {'iptv-native': self.binary.read_bytes(),
                            'native_worker.sh': b'old worker', 'native_status.sh': b'old status'}
                bundle = {'iptv-native': self.binary.read_bytes(),
                          'native_worker.sh': b'#!/bin/sh\necho new worker\n',
                          'native_status.sh': b'#!/bin/sh\necho new status\n'}
                for name in repair.FILES:
                    (base/name).write_bytes(original[name]); (base/name).chmod(0o755)
                    (work/name).write_bytes(bundle[name]); (work/name).chmod(0o755)
                (data/'ENABLED').touch(); (data/'PAUSED').touch()
                (data/'resources.txt').write_text('ROUTE_CLEANUP_FAILED 123 456 1\n')
                scripts = {
                    'uname': '#!/bin/sh\necho aarch64\n',
                    'cru': '#!/bin/sh\necho existing_cron_#IPTVHomeNative#\n',
                    'iptables': '#!/bin/sh\n[ "$3" = -S ] || exit 8\n',
                    'cp': '#!/bin/sh\ncase "$*" in *native_worker.sh.route-repair*) '
                          'if [ "$FAIL_COPY" = 1 ] && [ ! -f "$FAIL_MARK" ]; then touch "$FAIL_MARK"; exit 9; fi;; esac\n'
                          'exec /bin/cp "$@"\n'}
                for name, content in scripts.items():
                    (bins/name).write_text(content); (bins/name).chmod(0o755)
                with mock.patch.object(repair, 'BASE', str(base)), mock.patch.object(repair, 'DATA', str(data)):
                    script = repair.repair_script('a'*40, bundle)
                path = work/'repair.sh'; path.write_bytes(script)
                subprocess.run(['sh', '-n', str(path)], check=True)
                result = subprocess.run([str(self.binary), 'lock', str(data), str(legacy),
                                         '/bin/sh', str(path), str(work)], capture_output=True,
                                        env=dict(os.environ, PATH=str(bins)+':'+os.environ['PATH'],
                                                 FAIL_COPY=str(int(failed)), FAIL_MARK=str(root/'failed')))
                self.assertEqual(2 if failed else 0, result.returncode, result.stderr.decode())
                for name in repair.FILES:
                    self.assertEqual((original if failed else bundle)[name], (base/name).read_bytes())
                self.assertEqual(failed, (data/'PAUSED').exists())
                self.assertTrue((data/'ENABLED').exists())
                self.assertFalse(list(base.glob('*.route-repair.*')))
                backup = data/('before-route-retry-'+'a'*12)
                self.assertEqual(original['native_worker.sh'], (backup/'native_worker.sh').read_bytes())

    def test_download_requires_commit_and_rejects_bad_manifest(self):
        with self.assertRaises(ValueError):
            repair.prepare('master')
        with self.assertRaises(ValueError):
            repair.validate({'iptv-native': b'bad'}, b'source', {})
