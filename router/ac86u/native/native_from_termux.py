#!/usr/bin/env python3
"""PHONE ONLY. The AC86U executes only Shell/curl/the prebuilt native binary."""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HOST = 'wodeluyouqi@192.168.50.1'
BASE = '/opt/share/iptv-home-native'
DATA = '/opt/var/lib/iptv-home-native'
OLD = '/opt/var/lib/iptv-home-probe'
CONFIG = '/opt/etc/iptv-home-probe.json'
RUN = '/opt/share/iptv-home-probe/run.sh'
SERVICES = '/jffs/scripts/services-start'
BACKUP = OLD+'/before-native-v1'
START, END = '# BEGIN IPTV_HOME_PROBE', '# END IPTV_HOME_PROBE'
PREVIOUS_SCHEDULE = '*/5 * * * * /bin/sh '+BASE+'/native_run.sh'
# Cron has minute precision. Two invocations 30 seconds apart reuse the native
# guardian's existing locks: a busy sampler cannot launch another batch.
SCHEDULE = '* * * * * /bin/sh '+BASE+'/native_run.sh & sleep 30; /bin/sh '+BASE+'/native_run.sh'
PREVIOUS_BLOCK = START+'\ncru a IPTVHomeNative "'+PREVIOUS_SCHEDULE+'"\n'+END
BLOCK = START+'\ncru a IPTVHomeNative "'+SCHEDULE+'"\n'+END
NAMES = ('IPTVHomeProbe','IPTVHomePrimary','IPTVHomeRecheck','IPTVHomePeak','IPTVHomeResume','IPTVHomeThin','IPTVHomeNative')
POLICY = ('minimum_height_default','minimum_height_overrides','minimum_h264_stream_mbps',
          'minimum_hevc_stream_mbps','minimum_other_stream_mbps')
FILES = ('iptv-native','native_run.sh','native_worker.sh','native_status.sh')
SSH_OPTIONS = ['-p', '22', '-o', 'ConnectTimeout=10']


def remote_error(result):
    lines = result.stderr.decode('utf-8', errors='replace').splitlines()
    useful = [line for line in lines if line.strip() and not line.startswith('**')]
    return RuntimeError('\n'.join(useful[-4:]) or 'SSH/路由器命令失败，退出码 '+str(result.returncode))


@contextmanager
def ssh_session():
    """One authenticated phone connection per action; never store a password."""
    global SSH_OPTIONS
    previous = SSH_OPTIONS
    with tempfile.TemporaryDirectory(prefix='iptv-ssh-') as folder:
        socket = str(Path(folder)/'s')
        connection = previous+['-S', socket]
        try:
            print('请输入路由器登录密码，连接将用于本次全部步骤。', flush=True)
            # A background master may keep inherited descriptors open. Pipes
            # here would make communicate() wait for the master's lifetime.
            with tempfile.TemporaryFile() as errors:
                result = subprocess.run(['ssh', *connection, '-M', '-N', '-f',
                    '-o', 'ControlPersist=60', '-o', 'ServerAliveInterval=15',
                    '-o', 'ServerAliveCountMax=2', HOST], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errors)
                if result.returncode:
                    errors.seek(0)
                    result.stderr = errors.read()
                    raise remote_error(result)
            # A lost master must fail promptly, rather than silently opening
            # another authenticated connection or prompting for passwords.
            SSH_OPTIONS = connection+['-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
                                      '-o', 'ProxyCommand=false']
            yield
        finally:
            SSH_OPTIONS = previous
            try:
                subprocess.run(['ssh', *connection, '-o', 'BatchMode=yes', '-O', 'exit', HOST],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                # ControlPersist expires the private connection if close fails.
                pass


def ssh(command, data=None):
    result = subprocess.run(['ssh', *SSH_OPTIONS, HOST, command], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise remote_error(result)
    return result.stdout


def read(path):
    return ssh('cat '+shlex.quote(path))


def split_services(raw):
    text = raw.decode()
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ValueError('IPTV startup block is ambiguous')
    if START not in text:
        return text.rstrip()+'\n', '', ''
    before, tail = text.split(START)
    block, after = tail.split(END)
    return before, START+block+END, after


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def guard_hash(path, raw):
    return ('digest=$("$native" sha256 '+shlex.quote(path)+')\n'
            '[ "$digest" = '+sha(raw)+' ] || { echo '+
            shlex.quote('NATIVE_ERROR:file_changed:'+path)+' >&2; exit 2; }\n')


def token_config(path):
    if path.stat().st_mode & 0o077:
        raise ValueError('Token file must have mode 0600')
    token = path.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{20,255}', token):
        raise ValueError('Invalid token file')
    return ('header = "Authorization: Bearer '+token+'"\n').encode()


def package(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o700 if name.endswith('.sh') or name == 'iptv-native' else 0o600
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def stage(folder, token):
    binary = (folder/'iptv-native').read_bytes()
    manifest = json.loads((folder/'build.json').read_bytes())
    if (sha(binary) != manifest['binary_sha256'] or len(binary) != manifest['bytes'] or
            len(binary) > 65536 or manifest['architecture'] != 'aarch64' or
            sha((folder/'iptv_native.c').read_bytes()) != manifest['source_sha256']):
        raise ValueError('Native build does not match pinned sources')
    ssh('set -eu; [ "$(uname -m)" = aarch64 ]; [ "$(date +%z)" = +0800 ]; '
        '[ ! -e '+BACKUP+' ]; [ ! -e /opt/var/lib/iptv-home-thin/ENABLED ]; '
        '[ -f /opt/lib/ld-linux-aarch64.so.1 ]; /opt/bin/curl --version >/dev/null')
    original = read(CONFIG)
    config = json.loads(original)
    expected = json.loads((folder/'home-thin.json').read_bytes())
    if (config.get('probe_id') != expected['probe_id'] or config.get('route_context') != 'living-room-path-equivalent'
            or config.get('runtime_transport') != 'merlinclash-marked' or not config.get('protected_publishing_ready')
            or config.get('output_dir', OLD) != OLD):
        raise ValueError('Household identity/path/publishing contract mismatch')
    services, run, crontab = read(SERVICES), read(RUN), ssh('cru l')
    before, block, after = split_services(services)
    config['daily_worker_enabled'] = False
    run_guard = b'[ ! -f /opt/var/lib/iptv-home-probe/thin-mode.locked ] || exit 0\n'
    first, rest = run.split(b'\n',1)
    patched_run = run if run_guard in run else first+b'\n'+run_guard+rest
    files = {name:(folder/name).read_bytes() for name in FILES}
    files.update({'original-config':original, 'original-run':run, 'original-block':block.encode(),
        'original-crontab':crontab, 'original-services':services,
        'config':json.dumps(config).encode(), 'run':patched_run,
        'services':(before+BLOCK+after+'\n').encode(),
        'quality-policy':json.dumps({k:config[k] for k in POLICY if k in config}).encode(),
        'identity':(config['probe_id']+' 192.168.50.1\n').encode(), 'github.curl':token_config(token)})
    script = 'set -eu\numask 077\ncd "$1"\nnative="$PWD/iptv-native"\n'
    script += '[ ! -e '+BACKUP+' ]\n'+guard_hash(CONFIG,original)+guard_hash(RUN,run)+guard_hash(SERVICES,services)
    script += 'iptables -t nat -S merlinclash >/dev/null\nmkdir -p '+BACKUP+' '+BASE+' '+DATA+'/migration\n'
    for source, target in [('original-config','config.json'),('original-run','run.sh'),('original-block','startup-block'),('original-crontab','crontab'),('original-services','services-start')]:
        script += 'cp '+source+' snapshot; "$native" commit snapshot '+BACKUP+'/'+target+'\n'
    for name in FILES:
        script += 'cp '+name+' runtime; chmod 755 runtime; "$native" commit runtime '+BASE+'/'+name+'\n'
    script += 'printf "native migration\\n" > marker; "$native" commit marker '+OLD+'/thin-mode.locked\n'
    # Freeze the old wrapper/config before touching scheduled jobs.
    for source,target in [('run',RUN),('config',CONFIG),('services',SERVICES)]:
        script += 'cp '+source+' '+target+'.native-stage; chmod '+('755' if source != 'config' else '600')+' '+target+'.native-stage\n"$native" commit '+target+'.native-stage '+target+'\n'
    for name in NAMES:
        script += 'cru d '+name+' || { ! cru l | grep -Fq "#'+name+'#"; }\n'
    for source,target in [('quality-policy','migration/quality-policy.json'),('identity','identity'),('github.curl','github.curl')]:
        script += '"$native" commit '+source+' '+DATA+'/'+target+'\n'
    script += 'touch '+BACKUP+'/staged\nprintf "NATIVE_STAGED_DISABLED\\n"\n'
    files['stage.sh'] = script.encode()
    temp = '/opt/tmp/iptv-native-stage-'+sha(binary)[:16]
    command = ('set -eu; unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; '
        'export PATH=/opt/bin:/opt/sbin:/usr/sbin:/usr/bin:/sbin:/bin; umask 077; '
        'mkdir -p '+temp+' '+DATA+'; tar -xzf - -C '+temp+'; '
        +temp+'/iptv-native check; '+temp+'/iptv-native lock '+DATA+' '+OLD+' /bin/sh '+temp+'/stage.sh '+temp)
    try:
        print(ssh(command, package(files)).decode().strip())
    finally:
        # Contains a private curl credential: remove staging even on failure.
        try:
            ssh('rm -rf '+temp)
        except RuntimeError as error:
            print('暂存目录清理失败：'+str(error), file=sys.stderr)


def cloud_ready(token):
    header = token_config(token).decode().split('Bearer ',1)[1].split('"',1)[0]
    def fetch(branch, path):
        request = urllib.request.Request('https://api.github.com/repos/pppaaasss/-/contents/'+path+'?ref='+branch,
            headers={'Authorization':'Bearer '+header, 'Accept':'application/vnd.github.raw+json','User-Agent':'IPTV-phone-setup'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read(128*1024))
    config, status = fetch('master','config/home-thin.json'), fetch('home-control','status.json')
    if config.get('enabled') is not True or config.get('router_runtime') != 'native-v1' or status.get('history_migrated') is not True:
        raise ValueError('Cloud native controller/history not ready')


def activate(token, once):
    cloud_ready(token)
    command = ('set -eu; [ -f '+BACKUP+'/staged ]; [ -f '+OLD+'/thin-mode.locked ]; '
        '[ ! -f '+DATA+'/PAUSED ]; [ -f '+DATA+'/github.curl ]; ')
    if once:
        # Leave no recurring schedule. The ordinary worker enforces windows/budgets.
        command += '[ ! -f '+DATA+'/ENABLED ]; touch '+DATA+'/ENABLED; trap "rm -f '+DATA+'/ENABLED" EXIT HUP INT TERM; /bin/sh '+BASE+'/native_run.sh; /bin/sh '+BASE+'/native_status.sh'
    else:
        command += 'touch '+DATA+'/ENABLED; cru a IPTVHomeNative '+shlex.quote(SCHEDULE)+'; printf "NATIVE_ENABLED\\n"'
    print(ssh(command).decode().strip())


def extend_morning_window(raw):
    old = b'02|03|04|05|06|07|13|14|15|20|21|22'
    new = b'02|03|04|05|06|07|08|09|10|13|14|15|20|21|22'
    original = raw.replace(new, old)
    if sha(original) != 'c413e41d8f6c53e2334b35807d08665789335def3c86a171c2919aebce68f62a':
        raise ValueError('Native wrapper changed; refusing to overwrite it')
    return original.replace(old, new)


def poll30():
    """Update polling and extend the installed morning window through 11:00."""
    services = read(SERVICES)
    before, block, after = split_services(services)
    if block not in (PREVIOUS_BLOCK, BLOCK):
        raise ValueError('IPTV startup block changed; refusing to overwrite it')
    crontab = ssh('cru l')
    entries = [line.rsplit('#IPTVHomeNative#', 1)[0].strip()
               for line in crontab.decode().splitlines() if line.rstrip().endswith('#IPTVHomeNative#')]
    if len(entries) != 1 or entries[0] not in (PREVIOUS_SCHEDULE, SCHEDULE):
        raise ValueError('IPTV cron is missing or changed; inspect status first')
    run_path = BASE+'/native_run.sh'
    run = read(run_path)
    updated_run = extend_morning_window(run)
    updated = (before+BLOCK+after).encode()
    files = {'services.before': services, 'services.after': updated, 'crontab.before': crontab,
             'run.before': run, 'run.after': updated_run}
    script = ('set -eu\numask 077\ncd "$1"\nnative='+BASE+'/iptv-native\n'
              '[ -f '+DATA+'/ENABLED ]; [ ! -f '+DATA+'/PAUSED ]\n')
    script += guard_hash(SERVICES, services)+guard_hash(run_path, run)
    backup = DATA+'/before-poll30'
    script += 'mkdir -p '+backup+'\n'
    for name in ('services.before', 'crontab.before', 'run.before'):
        script += '[ -f '+backup+'/'+name+' ] || cp '+name+' '+backup+'/'+name+'\n'
    # /opt/tmp and /jffs can be different filesystems. Native commit uses
    # rename(2), so both forward writes and restores need destination siblings.
    script += ('install_file() {\n'
               '  staged="$2.poll30.$$"\n'
               # POSIX noclobber creates exclusively without a mktemp utility.
               '  (set -C; : > "$staged") || return 1\n'
               '  if cp "$1" "$staged" && chmod 755 "$staged" && '
               '"$native" commit "$staged" "$2"; then\n'
               '    return 0\n'
               '  fi\n'
               '  rm -f "$staged"\n'
               '  echo "POLL30_WRITE_FAILED:$2" >&2\n'
               '  return 1\n}\n')
    script += 'restore() {\n  trap - EXIT HUP INT TERM\n  restore_failed=0\n'
    script += '  install_file services.before '+SERVICES+' || restore_failed=1\n'
    script += '  install_file run.before '+run_path+' || restore_failed=1\n'
    script += '  cru a IPTVHomeNative '+shlex.quote(entries[0])+' || restore_failed=1\n'
    script += ('  if [ "$restore_failed" = 0 ]; then echo POLL30_RESTORED >&2; '
               'else echo POLL30_RESTORE_FAILED >&2; fi\n  exit 2\n}\n')
    script += 'trap restore EXIT HUP INT TERM\n'
    script += 'install_file run.after '+run_path+'\n'
    script += 'install_file services.after '+SERVICES+'\n'
    script += 'cru a IPTVHomeNative '+shlex.quote(SCHEDULE)+'\ntrap - EXIT HUP INT TERM\n'
    files['poll30.sh'] = script.encode()
    folder = '/opt/tmp/iptv-poll30-'+sha(services)[:16]
    command = ('set -eu; unset LD_LIBRARY_PATH LD_PRELOAD; '
               'export PATH=/opt/bin:/opt/sbin:/usr/sbin:/usr/bin:/sbin:/bin; '
               'umask 077; mkdir -p '+folder+'; tar -xzf - -C '+folder+'; '
               '/bin/sh '+folder+'/poll30.sh '+folder)
    try:
        ssh(command, package(files))
        if read(SERVICES) != updated:
            raise RuntimeError('Polling startup verification failed')
        if read(run_path) != updated_run:
            raise RuntimeError('Morning window verification failed')
        verified = ssh('cru l').decode().splitlines()
        if sum(line.rsplit('#IPTVHomeNative#',1)[0].strip() == SCHEDULE
               for line in verified if line.rstrip().endswith('#IPTVHomeNative#')) != 1:
            raise RuntimeError('Polling cron verification failed')
        print('POLL30_OK: polling every 30 seconds; morning window 02:00-11:00 Beijing; current worker locks retained')
    finally:
        ssh('rm -rf '+shlex.quote(folder))


def rollback():
    current_services, current_config, current_run = read(SERVICES), read(CONFIG), read(RUN)
    before, block, after = split_services(current_services)
    original_block = read(BACKUP+'/startup-block').decode()
    if block not in (BLOCK, PREVIOUS_BLOCK, original_block):
        raise ValueError('IPTV startup block changed; review backup first')
    prior_cron = read(BACKUP+'/crontab').decode()
    script = 'set -eu\numask 077\nnative='+BASE+'/iptv-native\n'+guard_hash(SERVICES,current_services)+guard_hash(CONFIG,current_config)+guard_hash(RUN,current_run)
    script += 'touch '+DATA+'/PAUSED; rm -f '+DATA+'/ENABLED; cru d IPTVHomeNative || true\n'
    for source,target in [('config.json',CONFIG),('run.sh',RUN)]:
        script += 'cp '+BACKUP+'/'+source+' '+target+'.native-restore; chmod '+('755' if source == 'run.sh' else '600')+' '+target+'.native-restore; "$native" commit '+target+'.native-restore '+target+'\n'
    script += 'cp '+DATA+'/restore-services '+SERVICES+'.native-restore; chmod 755 '+SERVICES+'.native-restore; "$native" commit '+SERVICES+'.native-restore '+SERVICES+'\n'
    for line in prior_cron.splitlines():
        for name in NAMES[:-1]:
            tag = '#'+name+'#'
            if line.rstrip().endswith(tag):
                script += 'cru a '+name+' '+shlex.quote(line.rsplit(tag,1)[0].strip())+'\n'
    script += 'rm -f '+OLD+'/thin-mode.locked\nprintf "LEGACY_RESTORED\\n"\n'
    ssh('umask 077; cat > '+DATA+'/restore-services; chmod 755 '+DATA+'/restore-services', (before+original_block+after).encode())
    ssh('umask 077; cat > '+DATA+'/rollback.sh', script.encode())
    print(ssh(BASE+'/iptv-native lock '+DATA+' '+OLD+' /bin/sh '+DATA+'/rollback.sh').decode().strip())


CHECK_CCTV10_ROUTES = [('原现用 183.129.255.66', 'http://183.129.255.66:8480/hls/11/index.m3u8'), ('历史源 101.66.195.43', 'http://101.66.195.43:9901/tsfile/live/0010_1.m3u8?key=txiptv&playlive=0&authid=0'), ('候选 115.48.161.223', 'http://115.48.161.223:9901/tsfile/live/0010_1.m3u8?key=txiptv&playlive=1&authid=0'), ('候选 101.66.194.200', 'http://101.66.194.200:9901/tsfile/live/0010_1.m3u8?key=txiptv&playlive=0&authid=0'), ('候选 101.66.195.190', 'http://101.66.195.190:9901/tsfile/live/0010_1.m3u8?key=txiptv&playlive=0&authid=0'), ('咪咕 09-13 链接', 'http://hlsztemgsplive.miguvideo.com:8080/wd_r2/2018/ocn/cctv10hd/1000/index.m3u8?msisdn=2026091322013939ba923b48fe4266a572b77627fff9d5&mdspid=&spid=699004&netType=0&sid=5500212874&pid=2028597139&timestamp=20260913220139&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=624878405&ParentNodeID=-99&assertID=5500212874&client_ip=171.8.79.254&SecurityKey=20260913220139&promotionId=&mvid=5100001696&mcid=500020&playurlVersion=ZQ-A1-9.9.1-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=5837149a266e69143e0e0a20b767b376'), ('咪咕 09-15 链接', 'http://hlsztemgsplive.miguvideo.com:8080/wd_r2/2018/ocn/cctv10hd/1000/index.m3u8?msisdn=20260915020138934930018d2f44b7aea1889124725733&mdspid=&spid=699004&netType=0&sid=5500212874&pid=2028597139&timestamp=20260915020138&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=624878405&ParentNodeID=-99&assertID=5500212874&client_ip=171.8.79.254&SecurityKey=20260915020138&promotionId=&mvid=5100001696&mcid=500020&playurlVersion=WX-A1-9.9.1-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=4de5cf556d20d1ddc199f4a275053fe2'), ('咪咕 09-14 链接', 'http://hlsztemgsplive.miguvideo.com:8080/wd_r2/2018/ocn/cctv10hd/1000/index.m3u8?msisdn=2026091402014016e7c0e0baa248da8d9512d239effe8c&mdspid=&spid=699004&netType=0&sid=5500212874&pid=2028597139&timestamp=20260914020140&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=624878405&ParentNodeID=-99&assertID=5500212874&client_ip=171.8.79.254&SecurityKey=20260914020140&promotionId=&mvid=5100001696&mcid=500020&playurlVersion=ZQ-A1-9.9.1-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=9871ed932e1fd940b593848afee8fdde'), ('咪咕 09-12 链接', 'http://hlsztemgsplive.miguvideo.com:8080/wd_r2/2018/ocn/cctv10hd/1000/index.m3u8?msisdn=202609122201397a0e2301b97c49589f84d93187eddf39&mdspid=&spid=699004&netType=0&sid=5500212874&pid=2028597139&timestamp=20260912220139&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=624878405&ParentNodeID=-99&assertID=5500212874&client_ip=171.8.79.254&SecurityKey=20260912220139&promotionId=&mvid=5100001696&mcid=500020&playurlVersion=ZQ-A1-9.9.1-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=b0765e98d93c04a5ed6a0cd7720d6414')]

CHECK_CCTV10_SHELL = r'''#!/bin/sh
set -eu
umask 077
work=$1
native=$IPTV_NATIVE_BASE/iptv-native
tab=$(printf '\t')
read -r probe dns < "$IPTV_NATIVE_DATA/identity"
[ "$dns" = 192.168.50.1 ] || exit 2
[ ! -f "$IPTV_NATIVE_DATA/PAUSED" ] || exit 2
mark=$((0x49600000 + ($$ % 65536)))
cleanup() { iptables -t nat -D OUTPUT -p tcp -m mark --mark "$mark/0xffffffff" -j merlinclash 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 75' HUP INT TERM
iptables -t nat -S merlinclash >/dev/null
iptables -t nat -I OUTPUT 1 -p tcp -m mark --mark "$mark/0xffffffff" -j merlinclash
while IFS="$tab" read -r index encoded; do
  current=$(printf '%s' "$encoded" | base64 -d)
  outcome=unsupported
  : > "$work/selection"
  for depth in 1 2 3; do
    "$native" get "$current" 98304 98304 "$work/playlist" "$work/metric" "$mark" "$dns" 53
    { printf 'M\t%s\t' "$index"; cat "$work/metric"; } >> "$work/results"
    IFS="$tab" read -r rc http size elapsed total complete final < "$work/metric"
    if [ "$rc" -ne 0 ] || [ "$http" -lt 200 ] || [ "$http" -ge 300 ]; then outcome=transfer_error; break; fi
    "$native" hls "$work/playlist" "$final" > "$work/selection" || break
    IFS="$tab" read -r kind next < "$work/selection"
    case "$kind" in
      MASTER) current=$next ;;
      SEGMENT) outcome=sampling; break ;;
      *) break ;;
    esac
  done
  if [ "$outcome" = sampling ]; then
    sample=0
    while IFS="$tab" read -r kind duration segment; do
      [ "$kind" = SEGMENT ] || break
      sample=$((sample+1)); [ "$sample" -le 2 ] || break
      "$native" get "$segment" 6291456 0 "$work/sample" "$work/metric" "$mark" "$dns" 53
      { printf 'S\t%s\t%s\t' "$index" "$duration"; cat "$work/metric"; } >> "$work/results"
      IFS="$tab" read -r rc http size rest < "$work/metric"
      if [ "$rc" -ne 0 ] || [ "$http" -lt 200 ] || [ "$http" -ge 300 ]; then outcome=transfer_error; break; fi
      if [ "$size" -lt 65536 ]; then outcome=short_sample; break; fi
      [ "$sample" -ne 2 ] || outcome=measured
    done < "$work/selection"
  fi
  printf 'E\t%s\t%s\n' "$index" "$outcome" >> "$work/results"
  sleep 2
done < "$work/routes"
'''


def parse_check_rows(raw, routes):
    results = {index: dict(index=index, label=label, url=url, samples=[], manifest=[],
                           outcome='not_completed', quality_verified=False)
               for index, label, url in routes}
    for line in raw.decode().splitlines():
        parts = line.split('\t')
        if len(parts) < 3 or int(parts[1]) not in results:
            raise ValueError('Unexpected diagnostic row')
        row = results[int(parts[1])]
        if parts[0] == 'E':
            row['outcome'] = parts[2]
            continue
        if parts[0] not in ('M', 'S'):
            raise ValueError('Unexpected diagnostic metric')
        start = 3 if parts[0] == 'S' else 2
        fields = parts[start:]
        if len(fields) != 7:
            raise ValueError('Incomplete diagnostic metric')
        metric = dict(curl_code=int(fields[0]), http_status=int(fields[1]), bytes=int(fields[2]),
                      seconds=float(fields[3]), total_bytes=int(fields[4]), final_url=fields[6])
        if parts[0] == 'S':
            metric['duration_s'] = float(parts[2])
            row['samples'].append(metric)
        else:
            row['manifest'].append(metric)
    for row in results.values():
        samples = row['samples']
        if row['outcome'] == 'measured' and len(samples) == 2:
            row['min_download_mbps'] = min(x['bytes']*8/max(x['seconds'], .001)/1e6 for x in samples)
            if all(x['duration_s'] > 0 and x['total_bytes'] > 0 for x in samples):
                row['stream_mbps'] = sum(x['total_bytes']*8 for x in samples)/sum(x['duration_s'] for x in samples)/1e6
                row['headroom_ratio'] = row['min_download_mbps']/row['stream_mbps']
    return list(results.values())


def check_cctv10():
    """Explicit one-off home transport check; no automatic qualification/publication."""
    import base64
    from datetime import datetime, timezone
    report = dict(schema='iptv-manual-cctv10-check/v1', home_probe=True,
                  production_use=False, quality_verified=False, rows=[],
                  started_utc=datetime.now(timezone.utc).isoformat())
    output = Path.home()/'cctv10-check.json'
    print('只检查 CCTV-10 的 9 个地址，每条最多两段、每段 6 MiB；不会自动换源。', flush=True)
    for offset in range(0, len(CHECK_CCTV10_ROUTES), 3):
        routes = [(i+1, *CHECK_CCTV10_ROUTES[i]) for i in range(offset, min(offset+3, len(CHECK_CCTV10_ROUTES)))]
        folder = '/opt/tmp/iptv-cctv10-check-'+os.urandom(8).hex()
        route_data = ''.join(str(i)+'\t'+base64.b64encode(url.encode()).decode()+'\n' for i, _label, url in routes)
        files = {'check.sh': CHECK_CCTV10_SHELL.encode(), 'routes': route_data.encode(), 'results': b''}
        prefix = ('set -eu; unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; '
                  'export PATH=/opt/bin:/opt/sbin:/usr/sbin:/usr/bin:/sbin:/bin; umask 077; ')
        error = ''
        try:
            ssh(prefix+'mkdir '+folder+'; tar -xzf - -C '+folder, package(files))
            command = (prefix+'export IPTV_NATIVE_BASE='+BASE+' IPTV_NATIVE_DATA='+DATA+'; '
                       '[ -f '+DATA+'/ENABLED ]; [ ! -f '+DATA+'/PAUSED ]; '
                       +BASE+'/iptv-native guard '+folder+'/resources 59392 51200 16384 240 '
                       '/bin/sh '+folder+'/check.sh '+folder)
            print('正在检查第 '+str(routes[0][0])+'–'+str(routes[-1][0])+' 条……', flush=True)
            try:
                ssh(command)
            except RuntimeError as exc:
                error = str(exc)
            rows = parse_check_rows(read(folder+'/results'), routes)
            report['rows'].extend(rows)
            for row in rows:
                message = row['outcome']
                if 'min_download_mbps' in row:
                    message = '两段下载完成，最低 %.2f Mbps' % row['min_download_mbps']
                    if 'headroom_ratio' in row:
                        message += '，节目约 %.2f Mbps，余量 %.2f 倍' % (row['stream_mbps'], row['headroom_ratio'])
                elif row['outcome'] == 'transfer_error':
                    metric = (row['samples'] or row['manifest'])[-1]
                    message = '传输异常 HTTP=%s curl=%s' % (metric['http_status'], metric['curl_code'])
                print('%d. %s：%s' % (row['index'], row['label'], message), flush=True)
            report['stop_reason'] = error
            report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            if error or any(row['outcome'] == 'not_completed' for row in rows):
                print('检查暂停：'+(error or '路由器正在执行其他任务，请稍后重试。'), flush=True)
                break
        finally:
            try:
                ssh('rm -rf '+folder)
            except RuntimeError:
                print('临时文件未能清理：'+folder, file=sys.stderr)
    completed = sum(row['outcome'] != 'not_completed' for row in report['rows'])
    print('CHECK_DONE %d/9；结果保存在 %s' % (completed, output), flush=True)
    print('这是家庭连接和下载检查，尚未验证分辨率、帧率或频道画面；请把结果截图发回来。', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Only the PHONE runs Python. Router receives a prebuilt native sampler.')
    parser.add_argument('action', choices=('stage','once','enable','pause','status','poll30','check-cctv10','rollback'))
    parser.add_argument('--token-file',type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action in ('stage','once','enable') and args.token_file is None:
        parser.error('--token-file is required (0600; never paste token into a command)')
    try:
        with ssh_session():
            if args.action == 'stage': stage(Path(__file__).resolve().parent,args.token_file)
            elif args.action in ('once','enable'): activate(args.token_file,args.action == 'once')
            elif args.action == 'pause': print(ssh('touch '+DATA+'/PAUSED; cru d IPTVHomeNative || true').decode())
            elif args.action == 'status': print(ssh('/bin/sh '+BASE+'/native_status.sh').decode())
            elif args.action == 'poll30': poll30()
            elif args.action == 'check-cctv10': check_cctv10()
            else: rollback()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('NATIVE_INSTALL_FAILED: '+str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
