#!/opt/bin/python3
"""Stage thin mode, freeze legacy scheduling, and preserve an explicit rollback.

No package repair, network access, token handling or history parsing occurs here.
Activation is a separate operation after cloud migration acknowledges completion.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path('/opt/share/iptv-home-thin')
LEGACY_BASE = Path('/opt/share/iptv-home-probe')
MANUAL_LOCK = Path('/opt/var/run/iptv-home-probe.lock')
ROOT = Path('/opt/var/lib/iptv-home-probe')
DATA = Path('/opt/var/lib/iptv-home-thin')
CONFIG = Path('/opt/etc/iptv-home-probe.json')
THIN_CONFIG = Path('/opt/etc/iptv-home-thin.json')
SERVICES = Path('/jffs/scripts/services-start')
BACKUP = ROOT / 'before-thin-v1'
NAMES = ('IPTVHomeProbe', 'IPTVHomePrimary', 'IPTVHomeRecheck', 'IPTVHomePeak', 'IPTVHomeResume')
POLICY = ('minimum_height_default', 'minimum_height_overrides', 'minimum_h264_stream_mbps',
          'minimum_hevc_stream_mbps', 'minimum_other_stream_mbps')
FILES = ('thin_probe.py', 'thin_api.py', 'thin_contract.py', 'thin_install.py', 'thin_run.sh',
         'thin_status.sh', 'home_probe.py', 'home_transport.py', 'home_resources.py',
         'home_contract.py', 'home_decision.py', 'candidate_history.py',
         'candidate_delivery.py', 'progress_journal.py', 'peak_policy.py')
START, END = '# BEGIN IPTV_HOME_PROBE', '# END IPTV_HOME_PROBE'
THIN_BLOCK = START + '\ncru a IPTVHomeThin "* * * * * /bin/sh /opt/share/iptv-home-thin/thin_run.sh"\n' + END


def write(path, raw, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.thin-tmp')
    with tmp.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(mode)
    tmp.replace(path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def split_services(text):
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ValueError('ambiguous IPTV startup block')
    if START not in text:
        return text.rstrip() + '\n', '', ''
    before, rest = text.split(START)
    body, after = rest.split(END)
    return before, START + body + END, after


def cron(*args):
    result = subprocess.run(['cru', *args], capture_output=True, text=True)
    if result.returncode and args[0] == 'd':
        listed = subprocess.check_output(['cru', 'l'], text=True)
        if '#' + args[1] + '#' not in listed:
            return ''  # Removing an already absent IPTV entry is idempotent.
    result.check_returncode()
    return result.stdout


def locks():
    stack = contextlib.ExitStack()
    try:
        for name in ('background-upgrade.lock', 'daily-worker.lock'):
            stream = stack.enter_context((ROOT / name).open('a'))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        DATA.mkdir(parents=True, exist_ok=True)
        stream = stack.enter_context((DATA / 'worker.lock').open('a'))
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if MANUAL_LOCK.exists():
            raise RuntimeError('legacy manual probe still owns its lock')
        return stack
    except BaseException:
        stack.close()
        raise


def install(bundle):
    legacy = json.loads(CONFIG.read_bytes())
    thin = json.loads((bundle / 'home-thin.json').read_bytes())
    if (legacy.get('probe_id') != thin['probe_id'] or legacy.get('route_context') != 'living-room-path-equivalent'
            or legacy.get('runtime_transport') != 'merlinclash-marked' or not legacy.get('protected_publishing_ready')):
        raise ValueError('household path, identity or publisher is not ready')
    if legacy.get('output_dir', str(ROOT)) != str(ROOT):
        raise ValueError('nonstandard history directory requires explicit migration mapping')
    for name in FILES:
        raw = (bundle / name).read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError('runtime file exceeds size limit')
        if name.endswith('.py'):
            compile(raw, name, 'exec')
    subprocess.run([sys.executable, '-I', '-B', '-c',
        'import sys; sys.path.insert(0,sys.argv[1]); import json,ssl,pathlib,threading,fcntl,thin_probe,home_probe,home_transport', str(bundle)], check=True)
    if not Path(legacy.get('ffprobe', '/opt/bin/ffprobe')).is_file():
        raise ValueError('FFprobe is unavailable')
    with locks():
        if BACKUP.exists() or THIN_CONFIG.exists():
            raise RuntimeError('thin setup already exists; inspect status or use rollback first')
        original = SERVICES.read_text() if SERVICES.exists() else '#!/bin/sh\n'
        before, block, after = split_services(original)
        previous_cron = cron('l')
        BACKUP.mkdir(mode=0o700)
        write(BACKUP / 'config.json', CONFIG.read_bytes())
        write(BACKUP / 'services-start', original.encode())
        write(BACKUP / 'startup-block', block.encode())
        write(BACKUP / 'crontab', previous_cron.encode())
        legacy_run = LEGACY_BASE / 'run.sh'
        write(BACKUP / 'run.sh', legacy_run.read_bytes(), 0o700)
        # Marker is checked by both the installed legacy wrapper and thin setup.
        write(ROOT / 'thin-mode.locked', b'thin migration in progress\n')
        patched_run = legacy_run.read_bytes()
        guard = b'[ ! -f /opt/var/lib/iptv-home-probe/thin-mode.locked ] || exit 0\n'
        if guard not in patched_run:
            first, remaining = patched_run.split(b'\n', 1)
            write(legacy_run, first + b'\n' + guard + remaining, 0o755)
        for name in NAMES:
            cron('d', name)
        legacy['daily_worker_enabled'] = False
        write(CONFIG, json.dumps(legacy).encode())
        write(SERVICES, (before + THIN_BLOCK + after + '\n').encode(), 0o755)
        BASE.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            write(BASE / name, (bundle / name).read_bytes(), 0o755 if name.endswith('.sh') else 0o644)
        thin['enabled'] = False
        write(THIN_CONFIG, json.dumps(thin).encode())
        write(DATA / 'migration/quality-policy.json', json.dumps({k:legacy[k] for k in POLICY if k in legacy}).encode())
        write(BACKUP / 'staged', b'STAGED\n')
        print('THIN_STAGED_DISABLED; legacy scheduling frozen; history preserved')


def activate():
    # Cloud acknowledgment and token validity are checked before a cron is added.
    sys.path.insert(0, str(BASE))
    from thin_api import GithubData, read_token
    api = GithubData(read_token('/opt/etc/iptv-home-thin.token'))
    remote = api.get('home-control', 'status.json')
    master = api.get('master', 'config/home-thin.json')
    if not remote or json.loads(remote).get('history_migrated') is not True:
        raise RuntimeError('cloud history migration has not completed')
    if not master or json.loads(master).get('enabled') is not True:
        raise RuntimeError('master thin controller is disabled')
    with locks():
        if not (BACKUP / 'staged').exists() or not (ROOT / 'thin-mode.locked').exists():
            raise RuntimeError('thin staging is incomplete')
        config = json.loads(THIN_CONFIG.read_bytes())
        config['enabled'] = True
        write(THIN_CONFIG, json.dumps(config).encode())
        (DATA / 'PAUSED').unlink(missing_ok=True)
        cron('a', 'IPTVHomeThin', '* * * * * /bin/sh /opt/share/iptv-home-thin/thin_run.sh')
        print('THIN_ENABLED; measurements wait for cloud tasks and resource admission')


def rollback():
    with locks():
        if not (BACKUP / 'config.json').exists():
            raise RuntimeError('no thin rollback snapshot')
        write(DATA / 'PAUSED', b'rollback\n')
        cron('d', 'IPTVHomeThin')
        if THIN_CONFIG.exists():
            thin = json.loads(THIN_CONFIG.read_bytes())
            thin['enabled'] = False
            write(THIN_CONFIG, json.dumps(thin).encode())
        before, block, after = split_services(SERVICES.read_text())
        original_block = (BACKUP / 'startup-block').read_text()
        if block not in (THIN_BLOCK, original_block):
            raise RuntimeError('IPTV startup block changed; restore it explicitly from backup')
        # Preserve unrelated startup edits made since installation.
        write(SERVICES, (before + original_block + after).encode(), 0o755)
        write(CONFIG, (BACKUP / 'config.json').read_bytes())
        write(LEGACY_BASE / 'run.sh', (BACKUP / 'run.sh').read_bytes(), 0o755)
        for line in (BACKUP / 'crontab').read_text().splitlines():
            for name in NAMES:
                tag = '#' + name + '#'
                if line.rstrip().endswith(tag):
                    cron('a', name, line.rsplit(tag, 1)[0].strip())
        (ROOT / 'thin-mode.locked').unlink(missing_ok=True)
        print('LEGACY_RESTORED; all thin and legacy history retained')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stage', 'activate', 'rollback'))
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('router administrator required')
    if args.action == 'stage':
        install(args.bundle)
    elif args.action == 'activate':
        activate()
    else:
        rollback()


if __name__ == '__main__':
    main()
