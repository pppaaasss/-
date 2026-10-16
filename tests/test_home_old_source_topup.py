import copy
import json

from router.ac86u.home_contract import make_candidate
from router.ac86u.thin_contract import timestamp
from scripts import home_thin_control as cloud
from scripts.home_old_source_topup import historical_candidates
from tests.test_daily_home_intake import DailyControllerTests


class HistoricalTopupTests(DailyControllerTests):
    def add_old(self, suffix, url=None):
        row = make_candidate(dict(name='CCTV-1', sources=['history'],
            url=url or 'https://old.example.com/' + suffix))
        self.state['tested'][row['candidate_id']] = {
            'last_checked_utc': '2026-09-10T18:00:00Z', 'result': row}
        return row

    def test_new_first_then_old_under_same_500_budget_preserves_history(self):
        self.prepare_daily()
        self.config['daily_old_source_topup'] = True
        old = self.add_old('good')
        before = copy.deepcopy(self.state['tested'][old['candidate_id']])
        first, _ = self.step(); self.deliver(first)
        new, _ = self.step()
        self.assertTrue(all(t.get('measurement_origin') != 'historical_recheck' for t in new['tasks']))
        self.deliver(new)
        new, _ = self.step(); self.deliver(new)
        old_batch, _ = self.step()
        self.assertEqual([old['candidate_id']], [t['candidate_id'] for t in old_batch['tasks']])
        self.assertEqual('historical_recheck', old_batch['tasks'][0]['measurement_origin'])
        self.assertEqual(9, self.state['native_budget']['candidates'])
        self.deliver(old_batch)
        idle, report = self.step()
        self.assertFalse(idle.get('tasks'))
        record = self.state['tested'][old['candidate_id']]
        self.assertEqual([before], record['previous_checks'])
        self.assertEqual('historical_recheck', record['measurement_origin'])
        self.assertEqual('historical_recheck', next(r for r in report['candidate_results']
            if r['candidate_id'] == old['candidate_id'])['measurement_origin'])
        snapshot = cloud.status_snapshot(self.state, idle, self.config, self.now)
        self.assertEqual(8, snapshot['daily_candidates']['new_measured'])
        self.assertEqual(1, snapshot['daily_candidates']['historical_rechecks_measured'])

    def test_old_sources_obey_veto_safety_current_and_day_dedup(self):
        self.prepare_daily()
        good = self.add_old('allowed')
        bad = self.add_old('veto')
        self.add_old('private', 'http://192.168.1.1/live')
        today = self.add_old('today')
        self.state['tested'][today['candidate_id']]['last_checked_utc'] = timestamp(self.now)
        feedback = self.root / 'config/home-route-feedback.json'
        feedback.write_text(json.dumps({'bad': {'cctv1': [dict(url=bad['url'])]}, 'good': {}}))
        current = cloud.unique_core_routes(self.formal)
        self.assertEqual([good['candidate_id']], [r['candidate_id'] for r in
            historical_candidates(self.state, current, feedback, self.now, 4)])

    def test_full_budget_does_not_issue_old_sources(self):
        self.prepare_daily(); self.config['daily_old_source_topup'] = True
        self.add_old('one')
        first, _ = self.step(); self.deliver(first)
        self.state['native_budget']['candidates'] = 500
        task, _ = self.step()
        self.assertFalse(task.get('tasks'))

    def test_no_historical_dispatch_after_1100(self):
        self.prepare_daily(); self.config['daily_old_source_topup'] = True
        self.add_old('one')
        from router.ac86u.thin_contract import epoch
        self.now = epoch('2026-09-15T03:01:00Z')
        task, _ = self.step()
        self.assertFalse(task.get('tasks'))
