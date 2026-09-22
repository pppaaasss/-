"""Run the native guardian against a fake firewall, including retries and kills."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MARK = 0x49600001
OWN = f'-A OUTPUT -p tcp -m mark --mark {MARK}/0xffffffff -j merlinclash\n'
OTHER = '-A OUTPUT -p tcp -m mark --mark 0xc0a83200/0xffffff00 -j merlinclash_EXT\n'


class RouteCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name)/'iptv-native'
        subprocess.run(['cc', '-Os', '-Wall', '-Wextra', '-Werror',
                        '-Wno-deprecated-declarations', '-o', str(cls.binary),
                        str(ROOT/'router/ac86u/native/iptv_native.c'), '-ldl', '-lm'], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.env = dict(os.environ, IPTV_NATIVE_DATA=str(self.data),
                        IPTV_NATIVE_LEGACY=str(self.data), ROUTE_TEST_ROOT=str(self.data),
                        PATH=str(self.data)+':'+os.environ['PATH'])
        (self.data/'rules').write_text(OTHER)
        fake = self.data/'iptables'
        fake.write_text('''#!/bin/sh
set -eu
cd "$ROUTE_TEST_ROOT"
printf '%s\\n' "$*" >> calls
if [ -f hang ]; then exec sleep 30; fi
if [ "$3" = -S ]; then
  if [ -f fail-read ]; then echo 'xtables lock held' >&2; exit 4; fi
  cat rules
elif [ "$3" = -D ]; then
  if [ -f fail-delete ]; then echo 'delete temporarily blocked' >&2; exit 4; fi
  grep -v -- '1231028225/0xffffffff' rules > next || true
  mv next rules
  if [ -f delete-race ]; then echo 'already removed' >&2; exit 1; fi
else
  echo 'unsupported check option' >&2; exit 2
fi
''')
        fake.chmod(0o755)

    def intent(self, rule=True):
        (self.data/'route-mark').write_text(str(MARK)+'\n')
        if rule:
            (self.data/'rules').write_text(OTHER+OWN)

    def command(self, action='route-clean'):
        return subprocess.run([str(self.binary), action, str(self.data)],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def guard(self, command='true', seconds=5):
        return subprocess.run([str(self.binary), 'guard', str(self.data/'resources.txt'),
                               '59392', '51200', '16384', str(seconds), '/bin/sh', '-c', command],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def test_idle_guard_never_queries_firewall_or_creates_pause(self):
        (self.data/'fail-read').touch()
        result = self.guard('echo WAITING_WINDOW > "$IPTV_NATIVE_DATA/ran"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.data/'ran').exists())
        self.assertFalse((self.data/'calls').exists())
        self.assertFalse((self.data/'PAUSED').exists())

    def test_cleanup_deletes_only_owned_exact_rule_and_verifies_absence(self):
        self.intent()
        self.assertEqual(0, self.command().returncode)
        self.assertEqual(OTHER, (self.data/'rules').read_text())
        self.assertFalse((self.data/'route-mark').exists())
        calls = (self.data/'calls').read_text()
        self.assertEqual(1, calls.count('-D OUTPUT'))
        self.assertNotIn('-C ', calls)

    def test_absent_rule_is_success_even_when_check_option_would_error(self):
        self.intent(rule=False)
        self.assertEqual(0, self.command().returncode)
        self.assertNotIn('-D ', (self.data/'calls').read_text())

    def test_failed_listing_retains_intent_and_recovers_on_next_guard(self):
        self.intent()
        (self.data/'fail-read').touch()
        self.assertEqual(75, self.guard('touch "$IPTV_NATIVE_DATA/ran"').returncode)
        self.assertFalse((self.data/'ran').exists())
        self.assertTrue((self.data/'route-mark').exists())
        self.assertFalse((self.data/'PAUSED').exists())
        self.assertIn('xtables lock held', (self.data/'route-error.txt').read_text())
        self.assertTrue((self.data/'resources.txt').read_text().startswith('ROUTE_CLEANUP_RETRY'))
        (self.data/'fail-read').unlink()
        self.assertEqual(0, self.guard('touch "$IPTV_NATIVE_DATA/ran"').returncode)
        self.assertTrue((self.data/'ran').exists())
        self.assertFalse((self.data/'route-mark').exists())

    def test_delete_failure_and_delete_race(self):
        self.intent()
        (self.data/'fail-delete').touch()
        self.assertEqual(75, self.command().returncode)
        self.assertTrue((self.data/'route-mark').exists())
        (self.data/'fail-delete').unlink()
        (self.data/'delete-race').touch()
        self.assertEqual(0, self.command().returncode)
        self.assertEqual(OTHER, (self.data/'rules').read_text())

    def test_killed_worker_route_is_reclaimed_using_persisted_intent(self):
        (self.data/'rules').write_text(OTHER+OWN)
        result = self.guard(f'echo {MARK} > "$IPTV_NATIVE_DATA/route-mark"; sleep 10', seconds=1)
        self.assertEqual(75, result.returncode)
        self.assertTrue((self.data/'resources.txt').read_text().startswith('TIME_LIMIT'))
        self.assertFalse((self.data/'route-mark').exists())
        self.assertEqual(OTHER, (self.data/'rules').read_text())

    def test_invalid_intent_is_retained_without_firewall_mutation(self):
        (self.data/'route-mark').write_text('3221762560\n')
        self.assertEqual(75, self.command().returncode)
        self.assertFalse((self.data/'calls').exists())

    def test_cleanup_timeout_is_bounded_and_retains_intent(self):
        self.intent()
        (self.data/'hang').touch()
        self.assertEqual(75, self.command().returncode)
        self.assertTrue((self.data/'route-mark').exists())
        self.assertIn('timed out', (self.data/'route-error.txt').read_text())

    def test_upgrade_recovers_only_confirmed_old_cleanup_pause(self):
        (self.data/'PAUSED').touch()
        profile = self.data/'resources.txt'
        profile.write_text('MEMORY_RESERVE\t123\t456\t1\n')
        self.assertEqual(2, self.command('recover-route-pause').returncode)
        self.assertTrue((self.data/'PAUSED').exists())
        profile.write_text('ROUTE_CLEANUP_FAILED\t123\t456\t1\n')
        (self.data/'fail-read').touch()
        self.assertEqual(2, self.command('recover-route-pause').returncode)
        (self.data/'fail-read').unlink()
        (self.data/'rules').write_text(OTHER+OWN)
        self.assertEqual(2, self.command('recover-route-pause').returncode)
        (self.data/'rules').write_text(OTHER)
        self.assertEqual(0, self.command('recover-route-pause').returncode)
        self.assertFalse((self.data/'PAUSED').exists())


if __name__ == '__main__':
    unittest.main()
