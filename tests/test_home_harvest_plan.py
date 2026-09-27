import copy
import json
from pathlib import Path
import tempfile
import unittest

from router.ac86u.home_contract import make_candidate
from scripts.build_home_candidate_manifest import build_manifest
from scripts.home_thin_control import new_state
from scripts.plan_home_harvest import plan_harvest


FORMAL = b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/live\n'
PROBE = 'home-test-probe'


def candidate(number, **changes):
    return make_candidate(dict(name='CCTV-1', url=f'https://source.test/{number}',
                               sources=['test-source'], **changes))


class HomeHarvestPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.feedback = Path(self.tmp.name) / 'feedback.json'
        self.feedback.write_text('{}')
        self.state = new_state(PROBE)
        self.state['migration_complete'] = True

    def plan(self, manifest=None):
        return plan_harvest(self.state, manifest, FORMAL, self.feedback, PROBE)

    def add(self, count):
        self.state['queue'] = {row['candidate_id']: row for row in
                               (candidate(i) for i in range(count))}

    def test_refill_below_800_and_use_existing_queue_at_or_above_800(self):
        for count, harvest in ((0, True), (799, True), (800, False), (801, False)):
            with self.subTest(count=count):
                self.add(count)
                before = copy.deepcopy(self.state)
                self.assertEqual((self.plan()['pending'], self.plan()['harvest']),
                                 (count, harvest))
                self.assertEqual(self.state, before)

    def test_tested_and_unschedulable_rows_do_not_fill_threshold(self):
        self.add(800)
        row = candidate(0)
        self.state['tested'][row['candidate_id']] = {'legacy': True}
        option = candidate(801, options='Referer=https://example.com')
        self.state['queue'][option['candidate_id']] = option
        current = make_candidate(dict(name='CCTV1', url='https://current.test/live', sources=['test']))
        self.state['queue'][current['candidate_id']] = current
        self.assertEqual(799, self.plan()['pending'])
        self.assertTrue(self.plan()['harvest'])

    def test_pending_manifest_is_counted_once_only_when_bound_to_current_playlist(self):
        self.add(799)
        manifest, _ = build_manifest(discovery_rows=[candidate(0), candidate(799)],
            formal_bytes=FORMAL, formal_url='https://example.com/tv.m3u',
            source_revision='test', generated_utc='2026-09-27T06:00:00Z')
        self.assertEqual(800, self.plan(manifest)['pending'])
        self.assertFalse(self.plan(manifest)['harvest'])
        manifest['formal_playlist']['sha256'] = '0' * 64
        self.assertEqual(799, self.plan(manifest)['pending'])

    def test_feedback_and_conflicting_channel_addresses_do_not_fill_threshold(self):
        self.add(801)
        self.feedback.write_text(json.dumps({'bad': {'CCTV1': [{'url': 'https://source.test/0'}]}}))
        conflict = make_candidate(dict(name='CCTV2', url='https://source.test/1', sources=['test']))
        self.state['archive'][conflict['candidate_id']] = conflict
        self.assertEqual(799, self.plan()['pending'])
        self.assertTrue(self.plan()['harvest'])

    def test_missing_or_unmigrated_history_triggers_discovery(self):
        self.assertTrue(plan_harvest(None, None, FORMAL, self.feedback, PROBE)['harvest'])
        self.add(800)
        self.state['migration_complete'] = False
        self.assertTrue(self.plan()['harvest'])

    def test_wrong_probe_history_cannot_skip_discovery(self):
        self.state['probe_id'] = 'another-probe'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.plan()


if __name__ == '__main__':
    unittest.main()
