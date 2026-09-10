import hashlib
import tempfile
import unittest
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.home_contract import make_candidate, validate_home_report_v2
from tests.test_ac86u_home_probe import NOW, measured


class LargeSourceBatchesTests(unittest.TestCase):
    def test_fast_failures_are_checkpointed_and_next_batch_resumes(self):
        formal = b'#EXTM3U\n#EXTINF:-1,CCTV-5\nhttps://current.test/5\n'
        rows = [make_candidate(dict(name='CCTV-5', url='https://candidate.test/' + str(n),
                                    sources=['uploaded-playlist'])) for n in range(601)]
        manifest = dict(formal_playlist=dict(sha256=hashlib.sha256(formal).hexdigest(), channel_count=1),
                        candidate_count=len(rows), generated_utc=home_probe.utc_text(NOW), candidates=rows)
        def reject(name, url, *, floor, **kwargs):
            return measured(name, url, floor, 'BAD')
        with tempfile.TemporaryDirectory() as root:
            config = dict(output_dir=root, probe_id='home-test', maximum_load1=10000,
                          minimum_mem_available_kib=1, candidate_manifest_url='https://repo.test/new',
                          playlist_url='https://repo.test/core', batch_phase='candidates')
            with mock.patch.object(home_probe, 'fetch_playlist', return_value=(formal, config['playlist_url'], .1)), \
                 mock.patch.object(home_probe, 'fetch_candidate_manifest', return_value=(manifest, b'large', 'https://repo.test/new')), \
                 mock.patch.object(home_probe, 'probe_route', side_effect=reject) as calls:
                report, state = home_probe.run(config, now_epoch=NOW)
                validate_home_report_v2(report)
                self.assertEqual(200, calls.call_count)
                self.assertEqual(401, len(state['candidate_queue']))
                self.assertEqual(200, len(state['tested_candidate_ids']))
                first = {c.args[1] for c in calls.call_args_list}
                calls.reset_mock()
                report, state = home_probe.run(config, now_epoch=NOW + 60)
                validate_home_report_v2(report)
                self.assertEqual(200, calls.call_count)
                self.assertEqual(201, len(state['candidate_queue']))
                self.assertTrue(first.isdisjoint(c.args[1] for c in calls.call_args_list))


if __name__ == '__main__':
    unittest.main()
