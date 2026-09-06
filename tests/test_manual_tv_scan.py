import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from router.ac86u import manual_tv_scan as scan


def playlist(*rows):
    return ('#EXTM3U\n' + ''.join('#EXTINF:-1,' + n + '\n' + u + '\n' for n, u in rows)).encode()


class ManualScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'result.json'
        self.core = playlist(('CCTV-1', 'http://old.test/one'), ('江苏卫视', 'http://old.test/js'))
        self.tv = playlist(('CCTV-1', 'http://old.test/one'), ('江苏卫视', 'http://tv.test/js'),
                           ('BBC', 'http://extra.test/bbc'))
        self.transport = SimpleNamespace(installed=True, dialer=SimpleNamespace(
            ipv4=2, ipv6=0, resolver=SimpleNamespace(diagnostics=lambda: {'A': {'errors': 0}})))

    @contextlib.contextmanager
    def context(self, config):
        self.assertEqual('merlinclash-marked', config['runtime_transport'])
        try:
            yield self.transport
        finally:
            self.transport.installed = False

    def run_scan(self, *, side_effect=None, raw=None, budget=1200):
        config = {'route_context': 'unverified', 'github_push_enabled': False}
        with mock.patch.object(scan.probe, 'transport_context', self.context), \
             mock.patch.object(scan.probe, 'resource_guard', return_value=''), \
             mock.patch.object(scan.probe, 'system_resources', return_value={}), \
             mock.patch.object(scan.probe, 'fetch_playlist', side_effect=[(self.tv, '', 0), (self.core, '', 0)]), \
             mock.patch.object(scan.probe, 'probe_route', side_effect=side_effect, return_value=raw or {
                 'status': 'GOOD', 'observed_status': 'GOOD', 'deep_checked': True, 'height': 1080,
                 'codec': 'h264', 'sample_count': 2, 'error': ''}) as probe, \
             mock.patch.object(scan.probe, 'run', side_effect=AssertionError('formal run called')), \
             contextlib.redirect_stdout(io.StringIO()):
            result = scan.scan(config, self.output, budget)
        self.assertEqual({'route_context': 'unverified', 'github_push_enabled': False}, config)
        self.assertEqual(result, json.loads(self.output.read_text()))
        return result, probe

    def test_actual_tv_urls_are_tested_and_unmanaged_channels_excluded(self):
        result, probe = self.run_scan()
        self.assertEqual('COMPLETE', result['state'])
        self.assertEqual(['江苏卫视'], result['differs_from_core'])
        self.assertEqual(['http://old.test/one', 'http://tv.test/js'], [c.args[1] for c in probe.call_args_list])
        self.assertEqual({'GOOD': 2}, result['counts'])
        self.assertTrue(result['temporary_rule_cleaned'])
        self.assertFalse(result['route_verified'])
        self.assertFalse(result['production_use'])
        self.assertNotIn('http://', json.dumps(result))
        self.assertTrue(all(c.kwargs['include_metadata'] for c in probe.call_args_list))

    def test_multiple_tv_routes_are_all_tested_and_missing_never_uses_core(self):
        self.tv = playlist(('CCTV-1', 'http://tv.test/a'), ('CCTV1', 'http://tv.test/b'))
        result, probe = self.run_scan()
        self.assertEqual(['http://tv.test/a', 'http://tv.test/b'], [c.args[1] for c in probe.call_args_list])
        self.assertEqual({'GOOD': 2, 'UNKNOWN': 1}, result['counts'])
        self.assertEqual('missing_from_tv', result['rows'][-1]['error'])
        self.assertEqual([], result['untested'])

    def test_same_station_4k_variant_keeps_its_quality_floor(self):
        self.tv = playlist(('江苏卫视', 'http://tv.test/js'), ('江苏卫视4K', 'http://tv.test/js4k'))
        result, probe = self.run_scan()
        self.assertEqual([1080, 2160], [c.kwargs['floor'] for c in probe.call_args_list])
        self.assertEqual(3, result['managed_routes'])
        self.assertEqual(2, result['managed_channels'])

    def test_missing_metadata_is_unknown_even_if_samples_are_good(self):
        result, _ = self.run_scan(raw={'status': 'GOOD', 'observed_status': 'GOOD', 'deep_checked': False})
        self.assertEqual({'UNKNOWN': 2}, result['counts'])

    def test_interrupt_preserves_results_and_cleans_transport(self):
        result, probe = self.run_scan(side_effect=scan.ScanStopped('interrupted'))
        self.assertEqual('STOPPED', result['state'])
        self.assertTrue(result['temporary_rule_cleaned'])
        self.assertEqual(['CCTV-1', '江苏卫视'], result['untested'])
        self.assertEqual(1, probe.call_count)

    def test_exhausted_budget_does_not_start_channel_tests(self):
        result, probe = self.run_scan(budget=0)
        probe.assert_not_called()
        self.assertEqual('time_budget', result['reason'])
        self.assertEqual(2, len(result['untested']))

    def test_invalid_core_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            scan.plan(self.tv, playlist(('BBC', 'http://extra.test/bbc')))
        with self.assertRaises(ValueError):
            scan.plan(self.tv, playlist(('CCTV-1', 'http://x.test'), ('CCTV1', 'http://y.test')))


if __name__ == '__main__':
    unittest.main()
