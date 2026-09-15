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
               '  staged=$(mktemp "$2.poll30.XXXXXX") || return 1\n'
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


def main():
    parser = argparse.ArgumentParser(description='Only the PHONE runs Python. Router receives a prebuilt native sampler.')
    parser.add_argument('action', choices=('stage','once','enable','pause','status','poll30','rollback'))
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
            else: rollback()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('NATIVE_INSTALL_FAILED: '+str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
