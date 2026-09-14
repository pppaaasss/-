"""Actual native HTTP/HLS bytes, offline cloud decoding and resource interruption."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
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

    def test_optional_budget_exhaustion_completes_formal_report(self):
        self.migrated();task,_=self.step();self.deliver(task)
        self.config['discovery_seconds']=1
        task,report=self.step();self.assertEqual('SLOT_COMPLETE',task['state']);self.assertEqual(2,report['summary']['good'])


class NativeInstallTests(unittest.TestCase):
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
