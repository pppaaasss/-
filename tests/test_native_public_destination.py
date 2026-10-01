"""Offline admission tests; instrumentation exists only in a temporary C unit.

No runtime/environment production bypass is introduced. The existing loopback
HTTP tests need a separate, test-only socket-address virtualization fixture.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'router/ac86u/native/iptv_native.c'


class NativePublicDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        directory = Path(cls.tmp.name)
        cls.binary = directory / 'native'
        subprocess.run(['cc', '-O1', '-Wno-deprecated-declarations', '-o', str(cls.binary), str(SOURCE), '-ldl', '-lm'], check=True)
        code = SOURCE.read_text()
        # Local compilation instrumentation: production file has no switches.
        code = code.replace('static int dns_query(', 'static int original_dns_query(', 1)
        index = code.index('static struct curl_slist *resolve(')
        code = code[:index] + '''
static const char *fixture_ip="8.8.8.8";
static int dns_query(const char *h,const char *d,int port,int kind,char ips[][INET6_ADDRSTRLEN],int count) {
    (void)h;(void)d;(void)port;(void)kind;
    if(count==0&&fixture_ip[0]){snprintf(ips[0],INET6_ADDRSTRLEN,"%s",fixture_ip);return 1;}
    return count;
}
''' + code[index:]
        index = code.index('static int get(')
        code = code[:index] + '''
static CURLcode fixture_perform(CURL *c,struct transfer *t) {
    (void)c;t->status=302;snprintf(t->location,sizeof t->location,"http://127.0.0.1/private");return CURLE_OK;
}
''' + code[index:]
        code = code.replace('code=cp(c);', 'code=fixture_perform(c,&t);')
        code = code.replace('int main(', 'int production_main(', 1)
        code += r'''
int main(int argc,char **argv){
    if(argc<3)return 2;
    if(!strcmp(argv[1],"predicate")){printf("%d\n",public_numeric(argv[2]));return 0;}
    if(!strcmp(argv[1],"resolve")){
        load_curl();fixture_ip=argv[2];int blocked=0;
        struct curl_slist *result=resolve("http://media.example/live","192.168.1.1",53,&blocked);
        printf("%d %d\n",blocked,result!=NULL);cl(result);return 0;
    }
    if(!strcmp(argv[1],"socket")){
        struct { struct curl_sockaddr a; char padding[128]; } target={0};
        struct transfer t={.status=302};
        if(strchr(argv[2],':')){
            target.a.family=AF_INET6;target.a.addrlen=sizeof(struct sockaddr_in6);
            inet_pton(AF_INET6,argv[2],&((struct sockaddr_in6 *)&target.a.addr)->sin6_addr);
        }else{
            target.a.family=AF_INET;target.a.addrlen=sizeof(struct sockaddr_in);
            inet_pton(AF_INET,argv[2],&((struct sockaddr_in *)&target.a.addr)->sin_addr);
        }
        target.a.socktype=SOCK_STREAM;
        int fd=open_socket(&t,CURLSOCKTYPE_IPCXN,&target.a);
        printf("%d %d %ld\n",fd==CURL_SOCKET_BAD,t.local_error,t.status);
        if(fd>=0)close(fd);return 0;
    }
    if(!strcmp(argv[1],"redirect")){argv[1]="get";return get(argc,argv);}
    return 2;
}
'''
        unit = directory / 'unit.c'
        unit.write_text(code)
        cls.unit = directory / 'unit'
        subprocess.run(['cc', '-O1', '-Wno-deprecated-declarations', '-o', str(cls.unit), str(unit), '-ldl', '-lm'], check=True)

    def invoke(self, *args):
        return subprocess.check_output([str(self.unit), *args], text=True).strip()

    def test_special_purpose_literals_rejected(self):
        denied = ['0.1.2.3','10.1.2.3','100.64.0.1','127.0.0.1','169.254.169.254','172.31.0.1','192.0.0.9','192.0.2.1','192.31.196.1','192.52.193.1','192.175.48.1','192.88.99.1','192.168.1.1','198.18.0.1','198.51.100.1','203.0.113.1','224.0.0.1','240.0.0.1','255.255.255.255','::','::1','::ffff:127.0.0.1','::ffff:8.8.8.8','64:ff9b::808:808','100::1','2001::1','2001:2::1','2001:db8::1','2002:0808:0808::1','2620:4f:8000::1','3ffe::1','3fff::1','fc00::1','fe80::1','ff02::1']
        for address in denied:
            with self.subTest(address=address):
                self.assertEqual(self.invoke('predicate', address), '0')
                self.assertEqual(self.invoke('socket', address), '1 1 0')

    def test_global_literals_allowed(self):
        for address in ['8.8.8.8','1.1.1.1','114.249.227.137','2001:4860:4860::8888','2409:8087:74d9:21::6','2606:4700:4700::1111']:
            with self.subTest(address=address):
                self.assertEqual(self.invoke('predicate', address), '1')

    def test_dns_answers_and_absent_safe_answer_fail_closed(self):
        for address in ['', '10.0.0.1', '169.254.169.254', '::1', '2001:db8::1']:
            self.assertEqual(self.invoke('resolve', address), '1 0')
        self.assertEqual(self.invoke('resolve', '8.8.8.8'), '0 1')
        self.assertEqual(self.invoke('resolve', '2409:8087:74d9:21::6'), '0 1')

    def get_metric(self, binary, command, url):
        with tempfile.TemporaryDirectory() as tmp:
            data, metric = Path(tmp)/'sample', Path(tmp)/'metric'
            subprocess.run([str(binary), command, url, '65536', '0', str(data), str(metric), '0', '192.168.1.1', '53'], check=True, timeout=5)
            fields = metric.read_text().strip().split('\t')
            self.assertEqual(fields[:2], ['1000', '0'])
            self.assertEqual(data.read_bytes(), b'')

    def test_literal_and_redirect_use_unknown_metric_not_exit(self):
        self.get_metric(self.binary, 'get', 'http://127.0.0.1/live')
        self.get_metric(self.binary, 'get', 'http://[::1]/live')
        self.get_metric(self.unit, 'redirect', 'http://8.8.8.8/live')

    def test_hls_segment_is_guarded_at_actual_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp)/'list'
            playlist.write_text('#EXTM3U\n#EXTINF:8,\nhttp://169.254.169.254/latest/metadata\n#EXTINF:8,\nhttp://127.0.0.1/segment2\n')
            result = subprocess.check_output([str(self.binary), 'hls', str(playlist), 'http://8.8.8.8/master.m3u8'], text=True)
            self.assertIn('http://169.254.169.254/latest/metadata', result)
            self.get_metric(self.binary, 'get', 'http://169.254.169.254/latest/metadata')


if __name__ == '__main__':
    unittest.main()
