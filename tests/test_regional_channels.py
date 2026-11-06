import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from router.ac86u.home_contract import station_key, make_candidate, candidate_id
from router.ac86u.regional_channels import CHANNELS, route_equivalence_key
from router.ac86u.thin_contract import encode, epoch, timestamp
from scripts.build_home_candidate_manifest import build_manifest, retain_delivery
from scripts.daily_home_intake import build_daily, collect_daily
from scripts.home_test_campaign import eligible_rows
from scripts.home_old_source_topup import historical_candidates
from scripts import home_thin_control as cloud
from scripts.publish_home_decisions import playlist_entries
from scripts.update_regional_playlists import blocks, block_key, update
from scripts.harvest_sources import parse_source
from tests.test_home_thin import ThinFixture

ROOT = Path(__file__).resolve().parents[1]


class RegionalIdentityTests(unittest.TestCase):
    def test_exact_identities_do_not_collapse_networks_or_regions(self):
        self.assertEqual(len(CHANNELS), 54)
        self.assertEqual(station_key('台视'), 'TTV.tw')
        self.assertEqual(station_key('TBSテレビ'), 'TBSTV.jp')
        self.assertEqual(station_key('TVBS新聞台'), 'TVBSNews.tw')
        self.assertEqual(station_key('NHK総合（東京）'), 'NHKGeneralTVTokyo.jp')
        for name in ('NHK総合', 'NHK综合', 'NHK WORLD-JAPAN', 'NHK総合（山形）', 'TBS NEWS', 'TOKYO MX2'):
            self.assertIsNone(station_key(name), name)
        self.assertNotEqual(station_key('読売テレビ'), station_key('日本テレビ'))
        self.assertNotEqual(station_key('毎日放送'), station_key('TBSテレビ'))

    def test_variant_dedup_keeps_real_identity_and_routing_ids(self):
        key = 'FTVNews.tw'
        a = 'http://media.example.com:80/a/playlist.m3u8?playlive=1&id=7'
        b = 'http://media.example.com/a/chunklist0.m3u8?id=7'
        self.assertEqual(route_equivalence_key(key,a), route_equivalence_key(key,b))
        self.assertNotEqual(route_equivalence_key(key,a), route_equivalence_key(key,b.replace('id=7','id=8')))
        self.assertEqual(route_equivalence_key('cctv1',a), a)
        self.assertEqual(route_equivalence_key('FujiTV.jp', 'https://fujitv4.mov3.co/hls/fujitv.m3u8'),
                         route_equivalence_key('FujiTV.jp', 'https://fujitv4.mov3.co/hls/fujitv.m3u8?checkedby:iptvcat.org'))
        formal = b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://core.example.com/live\n'
        manifest, summary = build_manifest(discovery_rows=[dict(name='民視新聞台',url=x,sources=['fixture']) for x in (a,b)],
            formal_bytes=formal, formal_url='https://example.com/core',source_revision='test',generated_utc='2026-11-06T00:00:00Z')
        row = manifest['candidates'][0]
        self.assertEqual(len(manifest['candidates']), 1)
        self.assertEqual(row['url'],a)
        self.assertEqual(row['candidate_id'],candidate_id(key,a))
        # A disappearing source cannot drop a pending regional delivery merely
        # because the new discovery snapshot happens to contain core rows only.
        delta = dict(manifest, candidates=[], candidate_count=0, scope=['cctv','provincial_satellite'])
        pending = {'pending_batches': {'unacknowledged': [row]}}
        retained,_ = retain_delivery(delta, {}, pending)
        self.assertEqual(retained['candidates'], [row])
        self.assertIn('taiwan', retained['scope'])

    def test_old_standalone_core_contract_still_imports_without_repo(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);import home_contract as h;assert h.station_key('CCTV-1')=='cctv1'"
        subprocess.run([sys.executable,'-I','-c',code,str(ROOT/'router/ac86u')],check=True,cwd='/tmp')

    def test_regional_curated_inputs_cannot_bypass_core_source_evidence(self):
        with self.assertRaisesRegex(ValueError, 'regional catalog'):
            collect_daily([],{},b'',Path('/does-not-exist'),0,
                          regional_rows=[dict(name='CCTV-1',url='https://a.example.com/live')])

    def test_upstream_referer_is_retained_for_native_compatibility_filter(self):
        rows = parse_source('#EXTM3U\n#EXTINF:-1,日本テレビ\n#EXTVLCOPT:http-referrer=https://mov3.co/\nhttps://ntv.example.com/live.m3u8\n', '日本', 'fixture')
        self.assertEqual(rows[0]['options'], '#EXTVLCOPT:http-referrer=https://mov3.co/')


class RegionalHomeFlowTests(ThinFixture):
    def prepare_regions(self):
        self.migrated()
        self.config.update(daily_verified_intake=True,stop_temporary_campaigns=True,
                           daily_candidates=500,candidate_mode='drain_queue',candidate_budget_mode='night_queue')
        raw = [dict(name=name,url=f'https://regional{i}.example.com/live.m3u8',sources=['curated:regional-sources'])
               for i,name in enumerate(('民視新聞台','中視','TBSテレビ','朝日放送テレビ'))]
        self.manifest,_ = build_daily(raw,self.state,self.formal,self.root/'config/home-route-feedback.json',self.now,
            [dict(source='curated:regional-sources',origin='curated_regional_catalog')])
        (self.root/'harvest/home-candidates.json').write_bytes(encode(self.manifest))

    def test_regions_dispatch_and_archive_without_becoming_core_or_switches(self):
        self.prepare_regions()
        self.assertFalse(self.manifest['home_verified'])
        before = (self.root/'tv-core.m3u').read_bytes()
        current,_ = self.step()
        self.assertTrue(all(r['role']=='current' for r in current['tasks']))
        self.deliver(current)
        candidate,_ = self.step()
        self.assertEqual({r['channel_key'] for r in candidate['tasks']},
                         {'FTVNews.tw','CTV.tw','TBSTV.jp','ABCTV.jp'})
        self.assertEqual(self.state['native_budget']['candidates'],4)
        self.deliver(candidate)
        _,report = self.step()
        self.assertTrue(all(r['candidate_id'] in self.state['archive'] for r in candidate['tasks']))
        self.assertEqual({d['channel_key'] for d in report['decisions']},{'cctv1','cctv2'})
        self.assertEqual((self.root/'tv-core.m3u').read_bytes(),before)

    def test_regions_obey_shared_500_cap_and_existing_window(self):
        self.prepare_regions()
        current,_ = self.step();self.deliver(current)
        self.state['native_budget']['candidates']=499
        candidate,_ = self.step()
        self.assertEqual(len(candidate['tasks']),1)
        self.assertEqual(self.state['native_budget']['candidates'],500)
        self.deliver(candidate)
        idle,_ = self.step();self.assertFalse(idle.get('tasks'))
        self.now=epoch('2026-09-15T03:01:00Z')
        idle,_=self.step();self.assertFalse(idle.get('tasks'))

    def test_options_and_measured_variants_stay_out_of_native_queue(self):
        self.migrated()
        a=make_candidate(dict(name='民視新聞台',url='https://m.example.com/a/playlist.m3u8',sources=['fixture']))
        b=make_candidate(dict(name='民視新聞台',url='https://m.example.com:443/a/chunklist0.m3u8',sources=['fixture']))
        opts=make_candidate(dict(name='日本テレビ',url='https://ntv.example.com/live.m3u8',
            options='#EXTVLCOPT:http-referrer=https://mov3.co/',sources=['fixture']))
        self.state['tested'][a['candidate_id']]={'result':a,'last_checked_utc':'2026-09-10T18:00:00Z'}
        accepted,excluded=eligible_rows([b,opts],self.state,self.formal,self.root/'config/home-route-feedback.json')
        self.assertFalse(accepted)
        self.assertEqual(excluded[b['candidate_id']],'equivalent_regional_route_history')
        self.assertEqual(excluded[opts['candidate_id']],'unsafe_address_or_request_options')
        self.state['tested'][b['candidate_id']]={'result':b,'last_checked_utc':'2026-09-11T18:00:00Z'}
        current=cloud.unique_core_routes(self.formal)
        self.assertEqual(len(historical_candidates(self.state,current,self.root/'config/home-route-feedback.json',self.now,4)),1)
        self.state['batches']['reserved']={'created_utc':timestamp(self.now),'tasks':[b]}
        self.assertFalse(historical_candidates(self.state,current,self.root/'config/home-route-feedback.json',self.now,4))


class RegionalPlaylistTests(unittest.TestCase):
    def test_generated_artifacts_are_repeatable_and_preserve_other_blocks(self):
        data=json.loads((ROOT/'config/regional-streams.json').read_text())
        selected={r['channel_key'] for r in data['channels'] if r.get('primary')}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir()
            (root/'config/regional-streams.json').write_text(json.dumps(data))
            base='#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"\n# channels=3\n'+\
                 '#EXTINF:-1 group-title="卫视台",CCTV-1\nhttp://core.example.com/keep\n'+\
                 '#EXTINF:-1 group-title="日本",NHK综合\nhttp://old.example.com/non-tokyo\n'+\
                 '#EXTINF:-1 group-title="台湾",民视新闻\nhttp://old.example.com/ftv\n'
            for name in ('tv.m3u','tv-all.m3u','tv-easy.m3u'): (root/name).write_text(base)
            (root/'tv-core.m3u').write_text('untouched core')
            (root/'tv-backup.m3u').write_text('untouched backup')
            first=update(root)
            output=(root/'tv.m3u').read_text()
            self.assertIn('http://core.example.com/keep\n',output)
            self.assertIn('日本",NHK综合\nhttp://old.example.com/non-tokyo\n',output)
            self.assertNotIn('http://old.example.com/ftv',output)
            for key in selected:
                meta=CHANNELS[key]
                self.assertIn('group-title="'+meta['group']+'",'+meta['name'],output)
                self.assertIn('tvg-id="'+meta['epg_id']+'"',output)
            self.assertIn('#EXTVLCOPT:http-referrer=https://mov3.co/',output)
            # The existing core publisher must ignore regional option lines.
            _, core_entries = playlist_entries(output.encode())
            self.assertEqual([r.key for r in core_entries], ['cctv1'])
            update(root)
            self.assertEqual((root/'tv.m3u').read_text(),output)
            self.assertEqual((root/'tv-core.m3u').read_text(),'untouched core')
            self.assertEqual((root/'tv-backup.m3u').read_text(),'untouched backup')
            self.assertEqual(first['local_backup_count'],sum(len(r.get('backups', [])) for r in data['channels']))


if __name__ == '__main__':
    unittest.main()
