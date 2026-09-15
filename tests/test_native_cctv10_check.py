import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, mock

from router.ac86u.native import native_from_termux as installer


class CCTV10CheckTests(TestCase):
    def test_exact_nine_routes_and_incomplete_results_are_not_success(self):
        self.assertEqual(9, len(installer.CHECK_CCTV10_ROUTES))
        self.assertEqual(9, len({url for label, url in installer.CHECK_CCTV10_ROUTES}))
        rows = installer.parse_check_rows(b'M\t1\t28\t0\t0\t14.0\t0\t0\thttp://example.test\n', [(1, 'test', 'http://example.test')])
        self.assertEqual('not_completed', rows[0]['outcome'])
        self.assertNotIn('min_download_mbps', rows[0])
        self.assertFalse(rows[0]['quality_verified'])

    def test_busy_guard_stops_without_running_extra_batches_or_changing_schedule(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(installer.Path, 'home', return_value=Path(folder)), \
             mock.patch.object(installer, 'read', return_value=b''), \
             mock.patch.object(installer, 'ssh', return_value=b'') as remote:
            installer.check_cctv10()
            report = json.loads((Path(folder)/'cctv10-check.json').read_text())
        self.assertEqual(3, len(report['rows']))
        self.assertTrue(all(row['outcome'] == 'not_completed' for row in report['rows']))
        self.assertEqual(3, remote.call_count)  # stage, guard, cleanup
        command = remote.call_args_list[1].args[0]
        self.assertIn('guard ', command)
        self.assertIn('59392 51200 16384 240', command)
        self.assertIn('IPTV_NATIVE_DATA='+installer.DATA, command)
        self.assertNotIn('cru ', command)
        self.assertNotIn('github.curl', command)

    def test_actual_shell_with_hls_error_and_non_hls_responses(self):
        payload = b'\x47'+b'a'*131071
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                if self.path == '/fail':
                    self.send_response(403); self.end_headers(); return
                body = (b'#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n' if self.path == '/hls'
                        else b'<html>not video</html>' if self.path == '/html' else payload)
                self.send_response(200); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                real = root/'real-native'
                subprocess.run(['cc', '-Os', '-Wno-deprecated-declarations', '-o', str(real),
                    str(Path(installer.__file__).parent/'iptv_native.c'), '-ldl', '-lm'], check=True)
                # Keep the real sampler and HLS parser; only the isolated local
                # fixture disables SO_MARK and records firewall operations.
                wrapper = root/'iptv-native'
                wrapper.write_text('#!/bin/sh\nif [ "$1" = get ]; then\nexec '+str(real)+
                    ' get "$2" "$3" "$4" "$5" "$6" 0 "$8" "$9"\nfi\nexec '+str(real)+' "$@"\n')
                wrapper.chmod(0o755)
                firewall = root/'iptables'
                firewall.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "'+str(root/'rules')+'"\n')
                firewall.chmod(0o755)
                # Observe gaps without sleeping in this fixture.
                sleep = root/'sleep'; sleep.write_text('#!/bin/sh\n[ "$1" = 2 ]\n'); sleep.chmod(0o755)
                (root/'identity').write_text('test-probe 192.168.50.1\n')
                (root/'results').touch()
                origin = 'http://127.0.0.1:'+str(server.server_port)
                routes = [(i+1, suffix, origin+'/'+suffix) for i, suffix in enumerate(('hls', 'fail', 'html'))]
                (root/'routes').write_text(''.join(str(i)+'\t'+base64.b64encode(url.encode()).decode()+'\n' for i, label, url in routes))
                script = root/'check.sh'; script.write_text(installer.CHECK_CCTV10_SHELL)
                subprocess.run(['/bin/sh', str(script), folder], check=True, timeout=20,
                    env=dict(os.environ, PATH=folder+':'+os.environ['PATH'], IPTV_NATIVE_BASE=folder, IPTV_NATIVE_DATA=folder))
                rows = installer.parse_check_rows((root/'results').read_bytes(), routes)
                self.assertEqual(['measured', 'transfer_error', 'unsupported'], [r['outcome'] for r in rows])
                self.assertEqual(2, len(rows[0]['samples']))
                self.assertGreater(rows[0]['min_download_mbps'], 0)
                self.assertEqual(403, rows[1]['manifest'][-1]['http_status'])
                self.assertFalse(rows[2]['samples'])
                self.assertTrue(all(not row['quality_verified'] for row in rows))
                rules = (root/'rules').read_text()
                self.assertIn('-I OUTPUT', rules); self.assertIn('-D OUTPUT', rules)
        finally:
            server.shutdown(); server.server_close(); thread.join()
