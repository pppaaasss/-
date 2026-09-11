#!/opt/bin/python3
"""Pinned, restartable upgrade transaction; preserve activation and all probe data."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = Path('/opt/share/iptv-home-probe')
ROOT = Path('/opt/var/lib/iptv-home-probe')
CONFIG = Path('/opt/etc/iptv-home-probe.json')
FILES = ('candidate_history.py', 'candidate_delivery.py', 'progress_journal.py',
         'home_probe.py', 'daily_worker.py', 'home_contract.py', 'home_decision.py',
         'home_resources.py', 'home_transport.py', 'peak_policy.py', 'push_home_report.py',
         'github_pair.py', 'activate.py', 'run.sh', 'status.sh', 'runtime_status.py',
         'runtime_audit.sh', 'manual_tv_scan.py', 'background_upgrade.py')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.upgrade.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.chmod(temp, 0o600)
    temp.replace(path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def download(url, target):
    error = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise RuntimeError('upgrade file too large')
            target.write_bytes(raw)
            return
        except Exception as exc:
            error = exc
    raise RuntimeError('Download failed: ' + str(error))


def restore(backup):
    """Caller holds both locks. Validate every rollback byte before replacing."""
    backup = backup.resolve()
    if backup.parent != ROOT.resolve() or not backup.name.startswith('before-background-'):
        raise RuntimeError('invalid rollback directory')
    transaction = backup / 'transaction.json'
    if transaction.exists():
        saved = json.loads(transaction.read_text())
        restore_files = FILES
    else:
        # Legacy updater saved only the files it changed. Explicit --rollback
        # selects that backup; never delete dependencies it did not replace.
        json.loads((backup / 'config.json').read_text())
        restore_files = tuple(name for name in FILES if (backup / name).exists())
        if not restore_files:
            raise RuntimeError('legacy backup has no runtime files')
        for name in restore_files:
            if name.endswith('.py'): compile((backup / name).read_text(), name, 'exec')
        names = (*restore_files, 'config.json')
        saved = {'hashes': {name: hashlib.sha256((backup / name).read_bytes()).hexdigest() for name in names}}

    for name, digest in saved['hashes'].items():
        if name not in (*FILES, 'config.json', 'installed-version.json'):
            raise RuntimeError('unknown rollback file')
        if hashlib.sha256((backup / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('rollback hash mismatch: ' + name)
    for name in restore_files:
        if name in saved['hashes']:
            target = BASE / (name + '.restore')
            shutil.copy2(backup / name, target); target.replace(BASE / name)
        elif (BASE / name).exists():
            (BASE / name).unlink()
    atomic_json(CONFIG, json.loads((backup / 'config.json').read_text()))
    version = ROOT / 'installed-version.json'
    if 'installed-version.json' in saved['hashes']:
        atomic_json(version, json.loads((backup / 'installed-version.json').read_text()))
    else:
        version.unlink(missing_ok=True)
    # This updater never mutates probe state or jobs: there is no older snapshot
    # to overwrite the progress saved by the preceding worker.
    atomic_json(ROOT / 'last-upgrade-recovery.json', dict(backup=backup.name, epoch=time.time()))
    (ROOT / 'background-upgrade.locked').unlink(missing_ok=True)


def recover(backup_name=None):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'background-upgrade.lock').open('a') as upgrade_lock:
        fcntl.flock(upgrade_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (ROOT / 'daily-worker.lock').open('a') as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = ROOT / 'background-upgrade.locked'
            if backup_name is None:
                if not marker.exists():
                    print('RECOVERY_NOT_NEEDED'); return
                try:
                    backup_name = json.loads(marker.read_text())['backup']
                except (ValueError, KeyError):
                    raise RuntimeError('legacy marker: select the matching before-background backup with --rollback')
            restore(ROOT / backup_name)
    print('RECOVERY_READY: configuration and code restored; saved probe progress retained.')


def apply(revision, schedule_only=False, bundle=None):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('a pinned commit SHA is required')
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'background-upgrade.lock').open('a') as upgrade_lock:
        fcntl.flock(upgrade_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = ROOT / 'background-upgrade.locked'
        if marker.exists():
            raise RuntimeError('unfinished upgrade: run --recover before updating')
        config = json.loads(CONFIG.read_text())
        if config.get('daily_worker_enabled') is not True:
            raise RuntimeError('worker disabled: inspect status and recover first')
        with tempfile.TemporaryDirectory(prefix='background-stage-', dir=str(ROOT)) as temp:
            stage = Path(temp)
            for name in FILES:
                if bundle is not None:
                    shutil.copy2(Path(bundle) / name, stage / name)
                else:
                    download('https://raw.githubusercontent.com/pppaaasss/-/' + revision + '/router/ac86u/' + name, stage / name)
                if name.endswith('.py'):
                    compile((stage / name).read_text(), name, 'exec')
            # The marker is durable before disabling or touching installed files.
            backup = ROOT / ('before-background-' + str(time.time_ns()))
            backup.mkdir()
            shutil.copy2(CONFIG, backup / 'config.json')
            for name in FILES:
                if (BASE / name).exists(): shutil.copy2(BASE / name, backup / name)
            if (ROOT / 'installed-version.json').exists():
                shutil.copy2(ROOT / 'installed-version.json', backup / 'installed-version.json')
            hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()}
            for path in backup.iterdir():
                with path.open('rb') as stream: os.fsync(stream.fileno())
            atomic_json(backup / 'transaction.json', dict(hashes=hashes, revision=revision))
            atomic_json(marker, dict(backup=backup.name, revision=revision, state='PREPARED'))
            atomic_json(CONFIG, dict(config, daily_worker_enabled=False))
            with (ROOT / 'daily-worker.lock').open('a') as worker_lock:
                fcntl.flock(worker_lock, fcntl.LOCK_EX)
                try:
                    for name in FILES:
                        target = BASE / (name + '.background-new')
                        shutil.copy2(stage / name, target); os.chmod(target, 0o755)
                        with target.open('rb') as stream: os.fsync(stream.fileno())
                        target.replace(BASE / name)
                    atomic_json(ROOT / 'installed-version.json', dict(revision=revision,
                        installed_epoch=time.time(), rollback=backup.name))
                    # Owner-approved temporary headroom policy, 2026-09-11.
                    # Original configuration remains available for rollback.
                    atomic_json(CONFIG, dict(config, minimum_headroom_ratio=1.05))
                    marker.unlink()
                except Exception:
                    restore(backup)
                    raise
            # Keep two complete rollback versions. Never delete a pending one.
            backups = sorted(ROOT.glob('before-background-*'), key=lambda p:p.stat().st_mtime)
            for old in backups[:-2]:
                if (old / 'transaction.json').exists(): shutil.rmtree(old)
    print('BACKGROUND_READY: pinned version installed; headroom=1.05; activation and schedule preserved.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--launch'); group.add_argument('--apply')
    group.add_argument('--recover', action='store_true'); group.add_argument('--rollback')
    parser.add_argument('--bundle', help='Locally staged pinned runtime files')
    parser.add_argument('--schedule-only', action='store_true', help='Compatibility flag; activation is always preserved')
    args = parser.parse_args()
    if args.recover or args.rollback:
        recover(args.rollback); return
    revision = args.launch or args.apply
    if not re.fullmatch('[0-9a-f]{40}', revision): parser.error('a pinned commit SHA is required')
    if args.launch:
        ROOT.mkdir(parents=True, exist_ok=True)
        env = {k:v for k,v in os.environ.items() if k not in ('LD_LIBRARY_PATH','LD_PRELOAD','PYTHONHOME','PYTHONPATH')}
        with (ROOT / 'background-upgrade.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-I', '-X', 'utf8', '-u', str(Path(__file__).resolve()), '--apply', revision] + (['--bundle', args.bundle] if args.bundle else []),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env)
        print('后台更新已开始，完成后输出 BACKGROUND_READY；原定时任务会自动接续。')
    else:
        apply(revision, bundle=args.bundle)


if __name__ == '__main__': main()
