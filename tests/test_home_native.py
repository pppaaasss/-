"""Actual native HTTP/HLS bytes, offline cloud decoding and resource interruption."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from scripts import home_native_evidence as wire
from scripts import home_thin_control as cloud
from scripts.publish_home_decisions import publish_latest
from tests.test_home_thin import ThinFixture
from router.ac86u.native import native_from_termux as installer

ROOT = Path(__file__).resolve().parents[1]


class NativeFixture:
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.build.name)
        cls.binary = cls.folder/'iptv-native'
        subprocess.run(['cc','-Os','-s','-Wall','-Wextra','-Werror','-Wno-deprecated-declarations',
            '-o',str(cls.binary),str(ROOT/'router/ac86u/native/iptv_native.c'),'-ldl','-lm'],check=True)
        cls.media = cls.folder/'fixture.ts'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=1920x1080:r=25',
            '-t','2','-c:v','libx264','-preset','ultrafast','-threads','1','-b:v','4M','-minrate','4M',
            '-maxrate','4M','-bufsize','4M','-x264-params','nal-hrd=cbr:force-cfr=1',
            '-f','mpegts',str(cls.media)],check=True)
        cls.media_bytes = cls.media.read_bytes()
        cls.prefix = cls.media_bytes[:wire.PREFIX_BYTES]

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()


class NativeHTTPTests(NativeFixture, unittest.TestCase):
    def setUp(self):
        data = self.media_bytes
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location','/media.ts'); self.end_headers(); return
                self.send_response(200)
                # Deliberately ignore Range: the sampler must bound its own read.
                self.send_header('Content-Length',str(len(data))); self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def get(self, url, limit=196608, keep=131072, mark=0, dns='127.0.0.1', port=1):
        out, metric = self.folder/'sample', self.folder/'metric'
        subprocess.run([str(self.binary),'get',url,str(limit),str(keep),str(out),str(metric),str(mark),dns,str(port)],check=True,timeout=20)
        return out.read_bytes(), metric.read_text().strip().split('\t')

    def test_redirect_ignored_range_keeps_prefix_and_counts_actual_body(self):
        raw, metric = self.get(self.url+'/redirect')
        self.assertEqual(self.prefix,raw)
        self.assertEqual(['0','200','196608'],metric[:3])
        self.assertEqual(len(self.media_bytes),int(metric[4]))
        self.assertEqual(self.url+'/media.ts',metric[6])
        self.assertEqual(1080,wire.media_metadata(raw)['height'])

    def test_direct_stream_is_not_rejected_as_oversized_manifest(self):
        raw,_ = self.get(self.url+'/media.ts',98304,98304)
        path=self.folder/'playlist';path.write_bytes(raw)
        result=subprocess.check_output([str(self.binary),'hls',str(path),self.url+'/main.m3u8'],text=True)
        self.assertEqual('DIRECT\n',result)

    def test_hls_resolution_priority_relative_query_and_two_distinct_segments(self):
        path=self.folder/'playlist'
        path.write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=640x360\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\nhigh.m3u8?q=1\n')
        result=subprocess.check_output([str(self.binary),'hls',str(path),self.url+'/dir/main.m3u8'],text=True)
        self.assertEqual('MASTER\t'+self.url+'/dir/high.m3u8?q=1\n',result)
        path.write_text('#EXTM3U\n#EXTINF:2,\n../a.ts?q=1\n#EXTINF:3,\nb.ts\n')
        result=subprocess.check_output([str(self.binary),'hls',str(path),self.url+'/dir/high.m3u8'],text=True)
        self.assertEqual(['SEGMENT\t2.000000\t'+self.url+'/a.ts?q=1','SEGMENT\t3.000000\t'+self.url+'/dir/b.ts'],result.splitlines())
        path.write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:2,\na.ts\n')
        self.assertEqual(b'UNSUPPORTED\n',subprocess.check_output([str(self.binary),'hls',str(path),self.url]))

    def test_explicit_lan_dns_a_aaaa_and_no_system_fallback(self):
        server=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);server.bind(('127.0.0.1',0));server.settimeout(5)
        seen=[]
        def serve():
            try:
                for _ in range(2):
                    query,address=server.recvfrom(512);kind=int.from_bytes(query[-4:-2],'big');seen.append(kind)
                    answer=b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x7f\x00\x00\x01' if kind==1 else b''
                    response=query[:2]+b'\x81\x80\x00\x01'+(b'\x00\x01' if answer else b'\x00\x00')+b'\x00\x00\x00\x00'+query[12:]+answer
                    server.sendto(response,address)
            finally: server.close()
        thread=threading.Thread(target=serve);thread.start()
        raw,metric=self.get(self.url.replace('127.0.0.1','household.invalid')+'/media.ts',port=server.getsockname()[1])
        thread.join();self.assertEqual([28,1],seen);self.assertEqual('0',metric[0]);self.assertEqual(self.prefix,raw)
        _,metric=self.get(self.url.replace('127.0.0.1','localhost')+'/media.ts')
        self.assertEqual('6',metric[0])  # localhost must not use system DNS.

    def test_missing_mark_rule_is_local_unknown_not_channel_failure(self):
        fake=self.folder/'iptables';fake.write_text('#!/bin/sh\nexit 1\n');fake.chmod(0o755)
        with mock.patch.dict(os.environ,{'PATH':str(self.folder)+':'+os.environ['PATH']}):
            raw,metric=self.get(self.url+'/media.ts',mark=0x49600001)
        self.assertEqual('1000',metric[0]);self.assertEqual(b'',raw)

    def test_guard_stops_only_its_process_group_and_records_rss(self):
        unrelated=subprocess.Popen(['sleep','10'])
        profile=self.folder/'profile'
        try:
            result=subprocess.run([str(self.binary),'guard',str(profile),'59392','51200','16384','1','/bin/sh','-c','sleep 10'],timeout=5)
            reason,peak,_,elapsed=profile.read_text().split()
            self.assertEqual(75,result.returncode);self.assertEqual('TIME_LIMIT',reason)
            self.assertGreater(int(peak),0);self.assertLess(int(peak),16384);self.assertLess(float(elapsed),3)
            self.assertIsNone(unrelated.poll())
        finally: unrelated.terminate();unrelated.wait()

    def test_guard_stops_memory_growth(self):
        profile=self.folder/'memory-profile'
        result=subprocess.run([str(self.binary),'guard',str(profile),'59392','51200','16384','5',
            shutil.which('python3'),'-c','import time; allocation=bytearray(32*1024*1024); time.sleep(10)'],timeout=8)
        reason,peak,_,_=profile.read_text().split()
        self.assertEqual(75,result.returncode);self.assertEqual('RSS_LIMIT',reason);self.assertGreater(int(peak),16384)

    def test_worker_recovers_last_durable_row_without_retesting(self):
        base=self.folder/'runtime';base.mkdir(exist_ok=True);shutil.copy(self.binary,base/'iptv-native')
        data=self.folder/'worker-data';data.mkdir(exist_ok=True)
        batch='a'*64;cycle='b'*64;record='R\t'+'c'*64+'\turl\t101\t102\t0\t-\tunsupported\t-\t-\t-\n'
        original=f'IPTV_NATIVE_V1\tprobe-test\t{batch}\t{cycle}\t100\n'+record
        (data/'outbox.native').write_text(original);(data/'outbox-id').write_text(batch+' 123\n')
        (data/'identity').write_text('probe-test 192.168.50.1\n');(data/'github.curl').write_text('')
        fake=self.folder/'curl';fake.write_text('#!/bin/sh\nprintf "503"\n');fake.chmod(0o755)
        env=dict(os.environ,IPTV_NATIVE_BASE=str(base),IPTV_NATIVE_DATA=str(data),PATH=str(self.folder)+':'+os.environ['PATH'])
        result=subprocess.run(['/bin/sh',str(ROOT/'router/ac86u/native/native_worker.sh')],env=env,capture_output=True)
        self.assertEqual(1,result.returncode)
        self.assertEqual(original+'END\t102\tinterrupted_batch\n',(data/'outbox.native').read_text())
        self.assertFalse((data/'last-uploaded').exists())



class NativeCloudTests(NativeFixture, ThinFixture):
    def change_metric(self, raw, column=6, field=1, value='600'):
        lines = raw.decode('ascii').splitlines()
        row = lines[1].split('\t')
        fields = base64.b64decode(row[column]).decode().strip().split('\t')
        fields[field] = value
        row[column] = wire.b64(('\t'.join(fields) + '\n').encode())
        lines[1] = '\t'.join(row)
        return ('\n'.join(lines) + '\n').encode()

    def asset(self, task, raw):
        inbox = self.root/'native-inbox'
        inbox.mkdir(exist_ok=True)
        path = inbox/(task['batch_id']+'-'+hashlib.sha256(raw).hexdigest()+'.native')
        path.write_bytes(raw)
        return path

    def capsule(self,task,statuses=None,tail='completed'):
        start=int(self.now+1);end=start+2
        lines=['\t'.join([wire.WIRE,self.probe,task['batch_id'],task['cycle_id'],str(start)])]
        for row in task['tasks']:
            good=(statuses or {}).get(row['channel_key'],'GOOD')=='GOOD'
            metric=wire.b64(f'0\t{200 if good else 503}\t100\t0.1\t100\t1\t{row["url"]}\t0\n'.encode())
            sample=wire.b64(f'0\t206\t131072\t0.1\t{len(self.media_bytes)}\t0\thttp://household.test/seg.ts\t2\n'.encode())
            lines.append('\t'.join(['R',row['task_id'],wire.b64(row['url'].encode()),str(start),str(end),'100',metric,
                'measured' if good else 'transfer_error',wire.b64(self.prefix) if good else '-',sample if good else '-',sample if good else '-']))
        lines.append(f'END\t{end}\t{tail}')
        return ('\n'.join(lines)+'\n').encode()

    def native_deliver(self,task,statuses=None):
        raw=self.capsule(task,statuses);inbox=self.root/'native-inbox';inbox.mkdir(exist_ok=True)
        name=task['batch_id']+'-'+hashlib.sha256(raw).hexdigest()+'.native';(inbox/name).write_bytes(raw)
        done=wire.prepare_assets(self.state,inbox,self.reports,self.now+10)
        self.assertIn(name,done);self.now+=4
        path=self.reports/'observations'/self.probe/(task['batch_id']+'.json')
        value=json.loads(path.read_bytes());self.assertFalse(value['native_evidence']['cloud_media_requests'])
        before=path.read_bytes();wire.prepare_assets(self.state,inbox,self.reports,self.now+20);self.assertEqual(before,path.read_bytes())
        return value

    def test_household_bytes_offline_metadata_and_protected_publication(self):
        self.migrated(); task,_=self.step()
        self.native_deliver(task,{'cctv1':'UNAVAILABLE'});task,_=self.step()
        self.assertEqual(2,task['tasks'][0]['attempt'])
        self.native_deliver(task,{'cctv1':'UNAVAILABLE'});task,_=self.step()
        self.assertEqual('candidate',task['tasks'][0]['role'])
        self.native_deliver(task);task,report=self.step()
        self.assertEqual('SLOT_COMPLETE',task['state']);self.assertEqual(1,report['summary']['replacements'])
        self.assertTrue(any(r['action']=='REPLACE' for r in report['decisions']))
        raw=json.dumps(report).encode();path=self.reports/'inbox'/self.probe/cloud.report_filename(report,raw)
        path.parent.mkdir(parents=True);path.write_bytes(raw)
        published=publish_latest(root=self.root,config_path=self.root/'config/home-publisher.json',
            inbox=self.reports/'inbox',now_epoch=self.now,apply=True)
        self.assertEqual(1,published['replacement_count'])
        self.assertIn(b'http://candidate.test/one.m3u8',(self.root/'tv-core.m3u').read_bytes())
        for observation in (self.reports/'observations'/self.probe).glob('*.json'):
            self.assertNotIn(wire.b64(self.prefix),observation.read_text())

    def test_wire_hash_and_reject_mismatched_media_identity(self):
        self.migrated();task,_=self.step();wirebytes=wire.task_wire(task,123)
        body,checksum=wirebytes.rsplit(b'SHA256\t',1)
        self.assertEqual(hashlib.sha256(body).hexdigest().encode(),checksum.strip())
        with self.assertRaises(ValueError): wire.read_native(self.capsule(task).replace(task['batch_id'].encode(),b'f'*64,1),task,self.now+10)
        with self.assertRaises(ValueError): wire.read_native(self.capsule(task)[:-10],task,self.now+10)
        self.assertEqual('IDLE\tDISABLED\n',wire.task_wire({'state':'DISABLED'},0).decode())

    def test_no_sample_is_unknown_and_interruption_never_refunds_unseen_work(self):
        self.migrated();task,_=self.step();raw=self.capsule(task).replace(wire.b64(self.prefix).encode(),b'-')
        value=wire.read_native(raw,task,self.now+10)
        for row in value['results']:
            expected=next(t for t in task['tasks'] if t['task_id']==row['task_id'])
            self.assertEqual('UNKNOWN',cloud.normalise(row['result'],expected)['observed_status'])
        reserved=self.state['native_budget']['bytes'];self.deliver(task,subset=0,stop='interrupted_batch');self.step()
        self.assertGreaterEqual(self.state['native_budget']['bytes'],reserved)

    def test_bad_http_status_is_unknown_while_valid_rows_and_budget_survive(self):
        self.migrated(); task,_ = self.step()
        reserved = self.state['native_budget']['bytes']
        raw = self.change_metric(self.capsule(task))
        path = self.asset(task, raw)
        self.assertEqual([path.name], wire.prepare_assets(self.state, path.parent, self.reports, self.now+10))
        observation = self.reports/'observations'/self.probe/(task['batch_id']+'.json')
        before = observation.read_bytes()
        value = json.loads(before)
        bad, good = value['results']
        self.assertEqual('UNKNOWN', bad['result']['observed_status'])
        self.assertEqual(0, bad['result']['sample_count'])
        self.assertFalse(bad['result']['deep_checked'])
        self.assertEqual('GOOD', good['result']['observed_status'])
        self.assertEqual(1080, good['result']['height'])
        self.assertIn('600', value['native_evidence']['rejected_measurements'][bad['task_id']]['reason'])
        self.assertTrue(value['native_evidence']['usage_complete'])
        self.assertNotIn(wire.b64(self.prefix), observation.read_text())
        self.now += 4
        cloud.ingest(self.state, self.reports, self.now)
        self.assertEqual(value['usage']['downloaded_bytes'], self.state['native_budget']['bytes'])
        self.assertEqual(value['usage']['runtime_s'], self.state['native_budget']['seconds'])
        self.assertNotIn(task['batch_id'], self.state['native_budget']['reservations'])
        next_task, report = self.step()
        self.assertIsNone(report)
        self.assertEqual([bad['task_id']], [key for key, row in self.state['cycle']['results'].items()
            if row['result']['observed_status'] == 'UNKNOWN'])
        self.assertEqual(['cctv1'], [row['channel_key'] for row in next_task['tasks']])
        self.assertGreaterEqual(self.state['native_budget']['bytes'], reserved)
        wire.prepare_assets(self.state, path.parent, self.reports, self.now+10)
        self.assertEqual(before, observation.read_bytes())

    def test_malformed_manifest_or_sample_never_qualifies(self):
        self.migrated(); task,_ = self.step()
        raw = self.capsule(task)
        for column, field, value in ((6,1,'600'), (9,1,'999'), (10,1,'-1'),
                (6,1,'nan'), (9,1,'200.5'), (9,2,'nan'), (10,3,'0')):
            with self.subTest(column=column, field=field, value=value), mock.patch.object(wire, 'media_metadata', return_value={}):
                parsed = wire.read_native(self.change_metric(raw,column,field,value), task, self.now+10)
                result = cloud.normalise(parsed['results'][0]['result'], task['tasks'][0])
                self.assertEqual('UNKNOWN', result['observed_status'])
                self.assertEqual('native_invalid_measurement', result['error'])
                self.assertEqual(field != 2, parsed['native_evidence']['usage_complete'])
                self.assertTrue(parsed['native_evidence']['usage_runtime_complete'])

    def test_invalid_byte_counter_keeps_bytes_but_settles_verified_completed_runtime(self):
        self.migrated(); task,_ = self.step()
        reserved = self.state['native_budget']['bytes']
        path = self.asset(task, self.change_metric(self.capsule(task), 9, 2, 'nan'))
        wire.prepare_assets(self.state, path.parent, self.reports, self.now+10)
        self.now += 4
        cloud.ingest(self.state, self.reports, self.now)
        self.assertEqual(reserved, self.state['native_budget']['bytes'])
        self.assertEqual(2, self.state['native_budget']['seconds'])
        before = json.dumps(self.state['native_budget'], sort_keys=True)
        cloud.ingest(self.state, self.reports, self.now)
        self.assertEqual(before, json.dumps(self.state['native_budget'], sort_keys=True))

    def test_interrupted_and_legacy_unknown_usage_still_keep_full_reservations(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.state = cloud.new_state(self.probe); self.migrated()
                task,_ = self.step()
                value = wire.read_native(self.change_metric(self.capsule(task, tail='interrupted_batch' if not legacy else 'completed')),
                    task, self.now+10)
                if legacy:
                    value['native_evidence'].update(usage_complete=False)
                    value['native_evidence'].pop('usage_bytes_complete')
                    value['native_evidence'].pop('usage_runtime_complete')
                folder = self.reports/'observations'/self.probe
                folder.mkdir(parents=True, exist_ok=True)
                for old in folder.glob('*.json'):
                    old.unlink()
                (folder/(task['batch_id']+'.json')).write_text(json.dumps(value))
                before = self.state['native_budget'].copy()
                self.now += 4
                cloud.ingest(self.state, self.reports, self.now)
                self.assertEqual(before['bytes'], self.state['native_budget']['bytes'])
                self.assertEqual(before['seconds'], self.state['native_budget']['seconds'])

    def test_unknown_candidate_advances_queue_without_replacement(self):
        self.migrated(); task,_ = self.step()
        self.native_deliver(task, {'cctv1':'UNAVAILABLE'}); task,_ = self.step()
        self.native_deliver(task, {'cctv1':'UNAVAILABLE'}); task,_ = self.step()
        self.assertEqual('candidate', task['tasks'][0]['role'])
        row = task['tasks'][0]
        path = self.asset(task, self.change_metric(self.capsule(task)))
        wire.prepare_assets(self.state, path.parent, self.reports, self.now+10)
        self.now += 4
        idle, report = self.step()
        self.assertEqual('SLOT_COMPLETE', idle['state'])
        self.assertEqual(0, report['summary']['replacements'])
        self.assertIn(row['candidate_id'], self.state['tested'])
        self.assertNotIn(row['candidate_id'], self.state['queue'])
        self.assertEqual(self.formal, (self.root/'tv-core.m3u').read_bytes())

    def test_invalid_asset_retained_and_unrelated_valid_batch_processed(self):
        self.migrated(); first,_ = self.step()
        bad = self.asset(first, self.capsule(first).replace(first['batch_id'].encode(), b'f'*64, 1))
        second,_ = self.step(seconds=1801)
        self.assertNotEqual(first['batch_id'], second['batch_id'])
        good = self.asset(second, self.capsule(second))
        for _ in range(2):
            self.assertEqual([good.name], wire.prepare_assets(self.state, good.parent, self.reports, self.now+10))
            self.assertFalse((self.reports/'observations'/self.probe/(first['batch_id']+'.json')).exists())
            self.assertTrue(bad.exists())
            rejected = self.reports/'native-rejections'/self.probe/(bad.name+'.json')
            self.assertEqual('native identity mismatch', json.loads(rejected.read_bytes())['reason'])
        self.now += 4
        self.step()
        self.assertEqual(2, self.state['last_heartbeat']['results'])

    def test_conflicting_evidence_never_overwrites_accepted_observation(self):
        self.migrated(); task,_ = self.step()
        self.native_deliver(task)
        observation = self.reports/'observations'/self.probe/(task['batch_id']+'.json')
        before = observation.read_bytes()
        conflicting = self.asset(task, self.change_metric(self.capsule(task)))
        done = wire.prepare_assets(self.state, conflicting.parent, self.reports, self.now+10)
        self.assertNotIn(conflicting.name, done)
        self.assertEqual(before, observation.read_bytes())
        self.assertTrue(conflicting.exists())

    def test_optional_budget_exhaustion_completes_formal_report(self):
        self.migrated();task,_=self.step();self.deliver(task)
        self.config['discovery_seconds']=1
        task,report=self.step();self.assertEqual('SLOT_COMPLETE',task['state']);self.assertEqual(2,report['summary']['good'])


class NativeHashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.build.name)
        cls.binary = cls.folder/'iptv-native'
        subprocess.run(['cc','-Os','-s','-Wall','-Wextra','-Werror','-Wno-deprecated-declarations',
            '-o',str(cls.binary),str(ROOT/'router/ac86u/native/iptv_native.c'),'-ldl','-lm'],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def test_hash_matches_standard_and_block_boundary_vectors_without_commands(self):
        samples = [b'', b'abc', b'a'*1000000]
        for size in (1,55,56,57,63,64,65,119,120,127,128,129,4095,4096,4097,131073):
            samples.append((bytes(range(256))*((size+255)//256))[:size])
        target = self.folder/'binary sample'
        for data in samples:
            with self.subTest(size=len(data)):
                target.write_bytes(data)
                result = subprocess.run([str(self.binary),'sha256',str(target)],
                    env={'PATH':''},capture_output=True,check=True)
                self.assertEqual(hashlib.sha256(data).hexdigest(),result.stdout.decode().strip())

    def test_hash_refuses_missing_file_and_read_error(self):
        for target,reason in ((self.folder/'missing',b'sha256_open'),(self.folder,b'sha256_read')):
            failed = subprocess.run([str(self.binary),'sha256',str(target)],capture_output=True)
            self.assertEqual(2,failed.returncode)
            self.assertIn(reason,failed.stderr); self.assertEqual(b'',failed.stdout)

    def test_install_guard_rejects_changed_file_without_external_hash_or_opkg(self):
        file = self.folder/'config with spaces'
        marker = self.folder/'mutated'
        original = b'{"daily_worker_enabled":true}\n'
        file.write_bytes(original)
        script = 'set -eu\nnative="'+str(self.binary)+'"\n'+installer.guard_hash(str(file),original)
        script += 'echo changed > "'+str(marker)+'"\n'
        subprocess.run(['/bin/sh','-c',script],env={'PATH':''},check=True,capture_output=True)
        marker.unlink(); file.write_bytes(b'changed concurrently')
        failed = subprocess.run(['/bin/sh','-c',script],env={'PATH':''},capture_output=True)
        self.assertEqual(2,failed.returncode); self.assertFalse(marker.exists())
        self.assertIn(b'NATIVE_ERROR:file_changed:',failed.stderr)
        file.unlink()
        failed = subprocess.run(['/bin/sh','-c',script],env={'PATH':''},capture_output=True)
        self.assertEqual(2,failed.returncode); self.assertFalse(marker.exists())
        self.assertIn(b'NATIVE_ERROR:sha256_open',failed.stderr)


class NativeInstallTests(unittest.TestCase):
    def test_background_connection_does_not_hold_install_output_open(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder)/'ssh'
            fake.write_text('#!'+sys.executable+'\n'+'''import os, pathlib, signal, sys, time
args = sys.argv[1:]
pidfile = pathlib.Path(args[args.index('-S')+1]+'.pid')
if '-M' in args:
    child = os.fork()
    if child == 0:
        os.setsid()
        time.sleep(8)
        os._exit(0)
    pidfile.write_text(str(child))
    os._exit(0)
elif '-O' in args:
    os.kill(int(pidfile.read_text()), signal.SIGTERM)
else:
    print('ok')
''')
            fake.chmod(0o755)
            began = time.monotonic()
            with mock.patch.dict(os.environ,{'PATH':folder+os.pathsep+os.environ['PATH']}):
                with installer.ssh_session():
                    self.assertEqual(b'ok\n',installer.ssh('status'))
            self.assertLess(time.monotonic()-began,4)

    def test_phone_reuses_one_session_and_closes_it_on_remote_error(self):
        previous = installer.SSH_OPTIONS
        ok = subprocess.CompletedProcess([],0,stdout=b'ok',stderr=b'')
        denied = subprocess.CompletedProcess([],2,stdout=b'',stderr=b'NATIVE_ERROR:worker_active\n')
        with mock.patch.object(installer.subprocess,'run',side_effect=[ok,ok,denied,ok]) as run:
            with self.assertRaisesRegex(RuntimeError,'NATIVE_ERROR:worker_active'):
                with installer.ssh_session():
                    self.assertEqual(b'ok',installer.ssh('first'))
                    installer.ssh('second')
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(1,sum('-M' in command for command in commands))
        for command in commands[1:3]:
            self.assertIn('BatchMode=yes',command)
            self.assertIn('ProxyCommand=false',command)
            self.assertEqual(commands[0][commands[0].index('-S')+1],command[command.index('-S')+1])
        self.assertIn('exit',commands[-1]); self.assertIs(previous,installer.SSH_OPTIONS)
        self.assertFalse(Path(commands[0][commands[0].index('-S')+1]).parent.exists())

    def test_prebuilt_binary_is_bound_to_reviewed_sources(self):
        root=ROOT/'router/ac86u/native';manifest=json.loads((root/'build.json').read_bytes())
        binary=(root/'iptv-native').read_bytes()
        self.assertEqual(manifest['binary_sha256'],hashlib.sha256(binary).hexdigest())
        self.assertEqual(manifest['source_sha256'],hashlib.sha256((root/'iptv_native.c').read_bytes()).hexdigest())
        self.assertEqual(183,int.from_bytes(binary[18:20],'little'))  # ELF EM_AARCH64
        self.assertEqual(manifest['bytes'],len(binary));self.assertLess(len(binary),65536)

    def test_phone_validation_rejects_ambiguous_startup_and_token_injection(self):
        with self.assertRaises(ValueError): installer.split_services(b'# BEGIN IPTV_HOME_PROBE\n')
        before,block,after=installer.split_services(b'#!/bin/sh\nVPN_START\n# BEGIN IPTV_HOME_PROBE\nold\n# END IPTV_HOME_PROBE\nOTHER_SERVICE\n')
        self.assertIn('VPN_START',before);self.assertIn('OTHER_SERVICE',after);self.assertIn('old',block)
        with tempfile.TemporaryDirectory() as folder:
            token=Path(folder)/'token';token.write_text('bad"\nurl = "https://example.invalid');token.chmod(0o600)
            with self.assertRaises(ValueError): installer.token_config(token)

    def test_router_bundle_contains_no_interpreter_or_decoder(self):
        self.assertEqual(('iptv-native','native_run.sh','native_worker.sh','native_status.sh'),installer.FILES)
        for name in installer.FILES[1:]:
            subprocess.run(['sh','-n',str(ROOT/'router/ac86u/native'/name)],check=True)
        self.assertNotIn('/opt/bin/python', (ROOT/'router/ac86u/thin_from_termux.sh').read_text())
