import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.candidate_history import prepare_history, unseen_candidates
from router.ac86u.home_contract import make_candidate
from tests.test_ac86u_home_probe import NOW, measured, seed_backup_pool


class HistoryTests(unittest.TestCase):
    def test_import_only_observed_id_and_preserve_current_progress(self):
        a = make_candidate(dict(sources=['history-test'], name='CCTV-1', url='https://old.test/1'))
        b = make_candidate(dict(sources=['history-test'], name='CCTV-1', url='https://new.test/1'))
        c = make_candidate(dict(sources=['history-test'], name='CCTV-1', url='https://done.test/1'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'pipeline-trial').mkdir()
            (root/'pipeline-trial/state.json').write_text(json.dumps({
                'candidate_observations': {a['candidate_id']: {'qualification': 'REJECTED'}},
                'candidate_queue': [b]}))
            state = {'candidate_queue': [a,b,c], 'candidate_observations': {
                c['candidate_id']: {'qualification': 'UNKNOWN'}}, 'current': {'keep': True}}
            result = prepare_history(state, root, 'home-test', NOW)
            self.assertEqual([b], result['candidate_queue'])
            self.assertEqual({'keep': True}, result['current'])
            self.assertEqual([], unseen_candidates([a,c], set(result['tested_candidate_ids'])))
            self.assertEqual(result, prepare_history(result, root, 'home-test', NOW+86400))

    def test_formal_unknown_is_not_requeued_and_manifest_change_does_not_rescan(self):
        formal=b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/1\n'
        candidate=make_candidate(dict(sources=['history-test'], name='CCTV-1', url='https://candidate.test/1'))
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(output_dir=tmp, probe_id='home-test', maximum_load1=10000,
                        minimum_mem_available_kib=1, candidate_manifest_url='https://repo.test/new',
                        playlist_url='https://repo.test/core', batch_phase='candidates')
            manifest={'formal_playlist': {'sha256':hashlib.sha256(formal).hexdigest(), 'channel_count':1},
                      'candidate_count':1, 'generated_utc':home_probe.utc_text(NOW), 'candidates':[candidate]}
            def observe(name,url,*,floor,**kw):
                return measured(name,url,floor,'UNKNOWN')
            with mock.patch.object(home_probe,'fetch_playlist',return_value=(formal,config['playlist_url'],.1)), \
                 mock.patch.object(home_probe,'fetch_candidate_manifest',return_value=(manifest,b'first','https://repo.test/new')), \
                 mock.patch.object(home_probe,'probe_route',side_effect=observe) as calls:
                _, state=home_probe.run(config,now_epoch=NOW)
                self.assertEqual(1,calls.call_count)
                self.assertEqual([],state['candidate_queue'])
            with mock.patch.object(home_probe,'fetch_playlist',return_value=(formal,config['playlist_url'],.1)), \
                 mock.patch.object(home_probe,'fetch_candidate_manifest',return_value=(manifest,b'changed','https://repo.test/new')), \
                 mock.patch.object(home_probe,'probe_route',side_effect=AssertionError('duplicate probe')):
                _, state=home_probe.run(config,now_epoch=NOW+86400)
                self.assertEqual([],state['candidate_queue'])

    def test_expired_backup_is_archived_without_scheduled_refresh(self):
        formal=b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/1\n'
        with tempfile.TemporaryDirectory() as tmp:
            seed_backup_pool(tmp,formal,[1])
            pool=json.loads((Path(tmp)/'qualified-backups.json').read_text())
            old=pool['backups'][0]
            config=dict(output_dir=tmp, probe_id='home-ac86u-test', maximum_load1=10000,
                        minimum_mem_available_kib=1, candidate_manifest_url='',
                        playlist_url='https://repo.test/core', batch_phase='final')
            def observe(name,url,*,floor,**kw):
                self.assertEqual('https://current.test/1',url)
                return measured(name,url,floor)
            with mock.patch.object(home_probe,'fetch_playlist',return_value=(formal,config['playlist_url'],.1)), \
                 mock.patch.object(home_probe,'probe_route',side_effect=observe):
                _,state=home_probe.run(config,now_epoch=NOW+40*3600)
            self.assertEqual(old,state['backup_archive'][old['candidate_id']])
            self.assertEqual([],state['candidate_queue'])
            self.assertEqual(0,state['qualified_backup_pool']['backup_count'])
