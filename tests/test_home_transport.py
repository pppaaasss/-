import contextlib
import functools
import http.server
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from router.ac86u import home_probe, home_transport as ht


def name(host):
    return b''.join(bytes([len(x)]) + x.encode() for x in host.split('.')) + b'\0'


def answer(ident=12, host='a.test', kind=1, cname=False):
    question = name(host) + struct.pack('!HH', kind, 1)
    header = struct.pack('!6H', ident, 0x8180, 1, 2 if cname else 1, 0, 0)
    address = ipaddress.ip_address('::1' if kind == 28 else '127.0.0.1').packed
    prefix = b''
    owner = b'\xc0\x0c'
    if cname:
        target = name('target.test')
        prefix = owner + struct.pack('!HHIH', 5, 1, 20, len(target)) + target
        owner = target
    return header + question + prefix + owner + struct.pack('!HHIH', kind, 1, 20, len(address)) + address


class DNSTests(unittest.TestCase):
    def test_empty_aaaa_is_distinct_from_failed_aaaa(self):
        resolver = ht.Landns()
        with mock.patch.object(resolver, 'query', side_effect=[([], 60), (['127.0.0.1'], 60)]) as query:
            self.assertEqual(['127.0.0.1'], resolver.resolve('a.test'))
            self.assertEqual(['127.0.0.1'], resolver.resolve('a.test'))
            self.assertEqual(2, query.call_count)
        self.assertEqual({'answers': 0, 'empty': 1, 'errors': 0}, resolver.diagnostics()['AAAA'])

    def test_partial_dns_failure_does_not_hide_family_recovery(self):
        resolver = ht.Landns()
        with mock.patch.object(resolver, 'query', side_effect=[
            TimeoutError('AAAA timeout'), (['127.0.0.1'], 60),
            (['::1'], 60), (['127.0.0.1'], 60),
        ]):
            self.assertEqual(['127.0.0.1'], resolver.resolve('a.test'))
            self.assertEqual(['::1', '127.0.0.1'], resolver.resolve('a.test'))
        self.assertEqual({'answers': 1, 'empty': 0, 'errors': 1}, resolver.diagnostics()['AAAA'])
        self.assertEqual(2, resolver.diagnostics()['A']['answers'])

    def test_total_dns_failure_remains_visible_and_is_not_cached(self):
        resolver = ht.Landns()
        with mock.patch.object(resolver, 'query', side_effect=TimeoutError('DNS timeout')):
            with self.assertRaises(OSError):
                resolver.resolve('a.test')
        self.assertEqual(1, resolver.diagnostics()['A']['errors'])
        self.assertEqual(1, resolver.diagnostics()['AAAA']['errors'])
        self.assertEqual({}, resolver.cache)

    def test_compressed_a_and_cname_answers(self):
        self.assertEqual((['127.0.0.1'], 20), ht.dns_reply(answer(cname=True), 12, 'a.test', 1))

    def test_aaaa_answer(self):
        self.assertEqual((['::1'], 20), ht.dns_reply(answer(kind=28), 12, 'a.test', 28))

    def test_wrong_id_and_question_are_rejected(self):
        for ident, host in [(13, 'a.test'), (12, 'wrong.test')]:
            with self.assertRaises(ValueError):
                ht.dns_reply(answer(), ident, host, 1)

    def test_truncated_and_circular_compression_are_rejected(self):
        with self.assertRaises(ValueError):
            ht.dns_reply(answer()[:-1], 12, 'a.test', 1)
        with self.assertRaises(ValueError):
            ht.dns_name(b'\xc0\x00', 0)

    def test_unrelated_answer_is_ignored(self):
        raw = answer().replace(b'\xc0\x0c', name('unrelated.test'))
        self.assertEqual([], ht.dns_reply(raw, 12, 'a.test', 1)[0])

    def test_dns_uses_explicit_tcp_server_and_handles_fragmented_reply(self):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                length = struct.unpack('!H', ht.exact(self.request, 2))[0]
                query = ht.exact(self.request, length)
                host, cursor = ht.dns_name(query, 12)
                kind = struct.unpack('!H', query[cursor:cursor+2])[0]
                data = answer(struct.unpack('!H', query[:2])[0], host, kind)
                framed = struct.pack('!H', len(data)) + data
                for index in range(0, len(framed), 5):
                    self.request.sendall(framed[index:index+5])
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(socket, 'getaddrinfo', side_effect=AssertionError('system DNS bypass')):
                    resolver = ht.Landns('127.0.0.1', server.server_address[1])
                    self.assertEqual(['::1', '127.0.0.1'], resolver.resolve('a.test'))
                    self.assertEqual(['::1', '127.0.0.1'], resolver.resolve('a.test'))
            finally:
                server.shutdown()
                thread.join()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        return super().do_GET()


class TransportNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root/'hello.txt').write_text('via household adapter')
        if shutil.which('ffmpeg'):
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc=size=160x120:rate=25',
                            '-t','3','-c:v','mpeg2video','-threads','1','-g','25',
                            '-hls_time','1','-hls_list_size','0',str(cls.root/'live.m3u8')],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        handler = functools.partial(QuietHandler, directory=str(self.root))
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.paths = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.resolver = mock.Mock()
        self.resolver.resolve.return_value = ['127.0.0.1']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_python_fetch_ignores_no_proxy_and_uses_adapter_dns(self):
        with mock.patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}):
            with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
                with transport.opener.open(f'http://channel.test:{self.port}/hello.txt', timeout=5) as response:
                    self.assertEqual(b'via household adapter', response.read())
                self.assertEqual(1, transport.dialer.ipv4)
        self.resolver.resolve.assert_called_with('channel.test')

    def test_origin_404_is_preserved(self):
        with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
            with self.assertRaises(urllib.error.HTTPError) as error:
                transport.opener.open(f'http://channel.test:{self.port}/missing', timeout=5)
            self.assertEqual(404, error.exception.code)
            self.assertIsNone(error.exception.headers.get('X-IPTV-Transport-Error'))
            error.exception.close()

    def test_dns_failure_is_infrastructure_unknown_not_dead_channel(self):
        self.resolver.resolve.side_effect = OSError('DNS failed')
        with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
            with mock.patch.object(home_probe, '_active_transport', transport):
                result = home_probe.probe_route('CCTV-1',f'http://channel.test:{self.port}/live.m3u8',floor=1080,config={})
                self.assertEqual('UNKNOWN', result['status'])
                self.assertIn('local_transport_failure', result['error'])

    def test_real_ffprobe_fetches_hls_playlist_and_segments_via_proxy(self):
        if not shutil.which('ffprobe') or not (self.root/'live.m3u8').exists():
            self.skipTest('FFmpeg tools unavailable')
        with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
            with mock.patch.object(home_probe, '_active_transport', transport):
                meta = home_probe.ffprobe_meta(f'http://channel.test:{self.port}/live.m3u8',shutil.which('ffprobe'))
            self.assertEqual(120, meta['height'])
            self.assertGreaterEqual(transport.dialer.ipv4, 2)
        self.assertIn('/live.m3u8', self.server.paths)
        self.assertTrue(any(p.endswith('.ts') for p in self.server.paths))

    def test_https_connect_retains_certificate_validation(self):
        if not shutil.which('openssl'):
            self.skipTest('openssl unavailable')
        cert, key = self.root/'cert.pem', self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=channel.test','-addext','subjectAltName=DNS:channel.test',
                        '-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert,key)
        self.server.socket = server_context.wrap_socket(self.server.socket,server_side=True)
        trusted = ssl.create_default_context(cafile=str(cert))
        with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
            opener = urllib.request.build_opener(ht.AlwaysProxy({'https':transport.url}),urllib.request.HTTPSHandler(context=trusted))
            with opener.open(f'https://channel.test:{self.port}/hello.txt',timeout=5) as response:
                self.assertEqual(b'via household adapter',response.read())
            if shutil.which('ffprobe') and (self.root/'live.m3u8').exists():
                with mock.patch.object(home_probe, '_active_transport', transport):
                    meta = home_probe.ffprobe_meta(f'https://channel.test:{self.port}/live.m3u8', shutil.which('ffprobe'))
                self.assertEqual(120, meta['height'])
            with self.assertRaises(urllib.error.URLError):
                transport.opener.open(f'https://channel.test:{self.port}/hello.txt',timeout=5)

    def test_ipv6_destination_works_without_ipv4_marking(self):
        class V6Server(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6
        try:
            server = V6Server(('::1', 0), functools.partial(QuietHandler, directory=str(self.root)))
        except OSError:
            self.skipTest('IPv6 loopback unavailable')
        server.paths = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.resolver.resolve.return_value = ['::1']
            with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
                with transport.opener.open(f'http://v6.test:{server.server_port}/hello.txt', timeout=5) as response:
                    self.assertTrue(response.read())
                self.assertEqual(1, transport.dialer.ipv6)
                self.assertEqual(0, transport.dialer.ipv4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_failed_ipv6_connection_falls_back_to_ipv4(self):
        self.resolver.resolve.return_value = ['::1', '127.0.0.1']
        with ht.HomeTransport(firewall=False, resolver=self.resolver) as transport:
            with transport.opener.open(f'http://channel.test:{self.port}/hello.txt', timeout=5) as response:
                self.assertTrue(response.read())
            self.assertEqual(1, transport.dialer.ipv4)


class FirewallTests(unittest.TestCase):
    def test_exception_removes_exact_rule(self):
        calls=[]
        def firewall(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if args[0]=='-C' else 0, '', '')
        with mock.patch.object(ht,'iptables',side_effect=firewall):
            with self.assertRaisesRegex(RuntimeError,'application failed'):
                with ht.HomeTransport():
                    raise RuntimeError('application failed')
        insert=next(x for x in calls if x[0]=='-I')
        self.assertIn(('-D','OUTPUT',*insert[3:]),calls)

    def test_existing_rule_is_not_deleted(self):
        with mock.patch.object(ht,'iptables',return_value=subprocess.CompletedProcess([],0,'','')) as firewall:
            with self.assertRaisesRegex(RuntimeError,'already in use'):
                with ht.HomeTransport():
                    pass
        self.assertFalse(any(c.args[0] in {'-I','-D'} for c in firewall.call_args_list))
