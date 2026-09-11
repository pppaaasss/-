"""Temporary 1.05 policy, historical evidence and on-demand household repairs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.candidate_history import prepare_history
from router.ac86u.home_contract import make_candidate
from tests.test_ac86u_home_probe import NOW, measured


class HeadroomTests(unittest.TestCase):
    def test_threshold_boundary_and_other_quality_failures(self):
        playlist=b'#EXTM3U\n#EXTINF:4,\none.ts\n#EXTINF:4,\ntwo.ts\n'
        for ratio, config, height, stream, expected in [
            (1.049, {}, 1080, 6, 'DEGRADED'),
            (1.05, {}, 1080, 6, 'GOOD'),
            (1.20, {'minimum_headroom_ratio':1.35}, 1080, 6, 'DEGRADED'),
            (1.35, {'minimum_headroom_ratio':1.35}, 1080, 6, 'GOOD'),
            (1.1, {}, 720, 6, 'DEGRADED'),
            (1.1, {}, 1080, 2.9, 'DEGRADED'),
        ]:
            with self.subTest(ratio=ratio,config=config,height=height,stream=stream):
                sample=dict(download_mbps=ratio*stream,stream_mbps=stream,
                            downloaded_bytes=512*1024,elapsed_s=.5)
                meta=dict(width=1920,height=height,codec='h264',fps=50,bitrate_mbps=stream)
                with mock.patch.object(home_probe,'fetch_playlist',return_value=(playlist,'https://source.test/live',.1)), \
                     mock.patch.object(home_probe,'segment_sample',return_value=sample), \
                     mock.patch.object(home_probe,'ffprobe_meta',return_value=meta):
                    row=home_probe.probe_route('CCTV-1','https://source.test/live',floor=1080,
                                               config=config,include_metadata=True)
                self.assertEqual(expected,row['observed_status'])

    def observation(self, suffix, **changes):
        candidate=make_candidate(dict(name='CCTV-1',url='https://backup.test/'+suffix,sources=['history']))
        verification=dict(height=1080,codec='h264',stream_mbps=6,headroom_ratio=1.1,
                          sample_count=2,deep_checked=True)
        verification.update(changes)
        return candidate['candidate_id'],dict(candidate=candidate,qualification='REJECTED',
            last_checked_utc=home_probe.utc_text(NOW-7*86400),
            result=dict(error='headroom_1.100_below_1.350',verification=verification))

    def test_old_rejections_become_archive_only_once_and_new_failures_win(self):
        accepted,observation=self.observation('accepted')
        rows=dict([self.observation('too-slow',headroom_ratio=1.049),
                   self.observation('low-height',height=720),self.observation('low-bitrate',stream_mbps=2),
                   self.observation('unknown',deep_checked=False),self.observation('one-sample',sample_count=1)])
        rows[accepted]=observation
        superseded,old=self.observation('superseded');rows[superseded]=old
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'pipeline-trial').mkdir()
            (root/'pipeline-trial/state.json').write_text(json.dumps({'candidate_observations':rows}))
            failure=copy.deepcopy(old);failure['result']['error']='HTTP Error 404: Not Found'
            state=prepare_history(dict(candidate_observations={superseded:failure},
                current_checkpoints={'old':{'attempts':{'cctv1':'old'}}},
                peak_failures={'cctv2':{'url':'https://peak.test/bad'}}),root,'home-test',NOW)
            self.assertEqual([accepted],list(state['backup_archive']))
            backup=state['backup_archive'][accepted]
            self.assertEqual(observation['last_checked_utc'],backup['last_checked_utc'])
            self.assertNotIn('qualification',backup)
            self.assertNotIn('expires_utc',backup)
            self.assertEqual({},state['current_checkpoints'])
            self.assertIn('cctv2',state['peak_failures'])
            self.assertIn(accepted,state['tested_candidate_ids'])
            self.assertEqual([],state['candidate_queue'])
            state['current_checkpoints']={'new':{'attempts':{'cctv1':'new'},'headroom_policy':1.05}}
            self.assertEqual(state,prepare_history(state,root,'home-test',NOW+3600))

    def test_recovered_backup_is_probed_before_switch_and_viewer_veto_blocks_it(self):
        identity,observation=self.observation('repair')
        formal=b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/1\n'
        for veto in (False,True):
            with self.subTest(veto=veto),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'state.json').write_text(json.dumps({'candidate_observations':{identity:observation}}))
                config=dict(output_dir=tmp,probe_id='home-test',maximum_load1=10000,
                    minimum_mem_available_kib=1,candidate_manifest_url='',batch_phase='final',
                    playlist_url='https://repo.test/core',minimum_headroom_ratio=1.05,actionable=True)
                url=observation['candidate']['url'];feedback={'bad':{'CCTV-1':[{'url':url}]} if veto else {},'good':{}}
                calls=[]
                def probe(name,url,*,floor,**kwargs):
                    calls.append(url)
                    return measured(name,url,floor,'DEGRADED' if 'current.test' in url else 'GOOD')
                with mock.patch.object(home_probe,'fetch_playlist',return_value=(formal,config['playlist_url'],.1)), \
                     mock.patch.object(home_probe,'load_home_feedback',return_value=(feedback,'loaded','0'*64)), \
                     mock.patch.object(home_probe,'probe_route',side_effect=probe):
                    report,state=home_probe.run(config,now_epoch=NOW)
                self.assertEqual(not veto,url in calls)
                self.assertEqual(0 if veto else 1,report['summary']['replacements'])
                self.assertEqual(1.05,report['policy']['minimum_headroom_ratio'])
                self.assertEqual([],state['candidate_queue'])
