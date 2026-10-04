import json
import unittest
from unittest import mock
from scripts import daily_home_intake as intake
from scripts import home_source_expansion as expansion
from tests.test_home_thin import ThinFixture
from router.ac86u.thin_contract import timestamp


class ExpansionTests(ThinFixture):
    def run_collect(self, mode='good', sources=None):
        self.migrated()
        if mode == 'history':
            from router.ac86u.home_contract import make_candidate
            row = make_candidate(dict(name='CCTV-1', url='https://safe.example.com/new', sources=['old']))
            self.state['tested'][row['candidate_id']] = {'result': row}
        calls = []
        def read(url, api=False):
            calls.append(url)
            if '/search/repositories?' in url:
                if mode == 'failure':
                    raise OSError('no service')
                return json.dumps({'items':[{'full_name':'owner/new', 'default_branch':'main',
                    'url':'http://127.0.0.1/evil'}]}).encode()
            if '/git/trees/' in url:
                return json.dumps({'tree':[
                    {'type':'blob','mode':'120000','path':'link.m3u'},
                    {'type':'blob','mode':'100644','path':'../escape.m3u'},
                    {'type':'blob','mode':'100644','path':'README.txt'},
                    {'type':'blob','mode':'100644','path':'live.m3u','size':300},
                    {'type':'blob','mode':'100644','path':'z.m3u','size':300}]}).encode()
            if '/commits?' in url:
                age = 90000 if mode == 'stale' and '/new/' in url else 60
                return json.dumps([{'sha':'a'*40,'commit':{'committer':{'date':timestamp(self.now-age)}}},
                    {'sha':'b'*40,'commit':{'committer':{'date':timestamp(self.now-3600)}}}]).encode()
            route = 'same' if mode == 'duplicate' else ('new' if '/new/' in url else 'same')
            if 'b'*40 in url:
                route += '-old'
            host = '127.0.0.1' if mode == 'unsafe' and '/new/' in url else 'safe.example.com'
            return ('#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://'+host+'/'+route).encode()
        sources = sources if sources is not None else [dict(url='https://raw.githubusercontent.com/owner/old/main/live.m3u')]
        with mock.patch.object(intake, 'LIMIT', 2):
            result = intake.collect_daily(sources,self.state,self.formal,
                self.root/'config/home-route-feedback.json',self.now,read)
        return result,calls

    def test_shortfall_searches_validates_and_stops_at_target(self):
        (m,s,e,f,a),calls=self.run_collect()
        self.assertEqual(m['candidate_count'],2)
        self.assertEqual(a['stop_reason'],'target_reached')
        self.assertEqual(a['registry_selected'],1)
        self.assertEqual(e[-1]['origin'],'search')
        self.assertIn('discovery_query',e[-1])
        self.assertFalse(any('z.m3u' in u or '127.0.0.1' in u or 'escape' in u or 'link.m3u' in u for u in calls))
        self.assertEqual(s['identity_merges'],0)
        self.assertFalse(m['production_eligible'])

    def test_repository_activity_does_not_admit_stale_file(self):
        (m,s,e,f,a),calls=self.run_collect('stale')
        self.assertEqual(m['candidate_count'],1)
        self.assertTrue(any(x['reason']=='playlist_not_updated_within_24h' for x in f))
        self.assertEqual(a['stop_reason'],'bounded_search_results_exhausted')

    def test_duplicate_discovery_does_not_fill_target(self):
        (m,s,e,f,a),calls=self.run_collect('duplicate')
        self.assertEqual(m['candidate_count'],1)
        self.assertGreater(s['normalization_merged_or_invalid'],0)

    def test_search_does_not_bypass_history_or_private_address_filter(self):
        for mode, reason in [('history','formal_queue_archive_or_tested_history'),
                             ('unsafe','unsafe_address_or_request_options')]:
            (m,s,e,f,a),calls=self.run_collect(mode)
            self.assertEqual(m['candidate_count'],1)
            self.assertEqual(s['history_and_safety_exclusions'][reason],1)
            self.assertFalse(any('https://127.0.0.1' in u for u in calls))

    def test_search_failure_preserves_registry_and_reports_failure(self):
        (m,s,e,f,a),calls=self.run_collect('failure')
        self.assertEqual(m['candidate_count'],1)
        self.assertEqual(a['stop_reason'],'search_failed')
        self.assertTrue(a['errors'])

    def test_total_request_and_file_limits_include_search(self):
        with mock.patch.object(expansion,'MAX_REQUESTS',4):
            (m,s,e,f,a),calls=self.run_collect()
        self.assertEqual(len(calls),4)
        self.assertEqual(a['stop_reason'],'request_budget_exhausted')
        with mock.patch.object(expansion,'MAX_FILES',1):
            (m,s,e,f,a),calls=self.run_collect()
        self.assertEqual(a['stop_reason'],'file_budget_exhausted')
        self.assertFalse(any('/search/' in u for u in calls))

    def test_sufficient_registry_does_not_search(self):
        sources=[dict(url='https://raw.githubusercontent.com/owner/'+name+'/main/live.m3u') for name in ['old','new']]
        (m,s,e,f,a),calls=self.run_collect(sources=sources)
        self.assertEqual(m['candidate_count'],2)
        self.assertFalse(a['attempted'])
        self.assertFalse(any('/search/' in u for u in calls))


class TransportSafetyTests(unittest.TestCase):
    def test_no_remote_host_or_redirect_can_receive_requests(self):
        for url in ['http://api.github.com/x','https://api.github.com.evil/x',
                    'https://user@api.github.com/x','https://127.0.0.1/x',
                    'https://api.github.com:443/x']:
            with self.assertRaisesRegex(ValueError,'untrusted'):
                intake.read_url(url,True)
        with self.assertRaisesRegex(ValueError,'redirect'):
            intake.NoRedirect().redirect_request(None,None,302,'',{},'http://127.0.0.1')

    def test_elapsed_budget_is_bounded(self):
        clock=mock.Mock(side_effect=[0,1201])
        transport=mock.Mock()
        b=expansion.ReadBudget(transport,clock)
        with self.assertRaisesRegex(ValueError,'time_budget'):
            b('https://api.github.com/x',True)
        transport.assert_not_called()
