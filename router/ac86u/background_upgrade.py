#!/opt/bin/python3
"""One detached field upgrade. Finish the active batch before replacing workers."""
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
FILES = ('candidate_history.py', 'home_probe.py', 'daily_worker.py', 'activate.py', 'run.sh')
ACK_NAME = '20260907T050003Z-recheck-1300-c91897fa4aad3518.json'
ACK_URL = 'https://raw.githubusercontent.com/pppaaasss/-/home-reports/inbox/home-ac86u-8f8908f0fba9/' + ACK_NAME


def atomic_json(path, value):
    temp = path.with_suffix('.upgrade.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temp, 0o600)
    temp.replace(path)


def download(url, target):
    error = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024)
            target.write_bytes(raw)
            return
        except Exception as exc:
            error = exc
    raise RuntimeError('Download failed: ' + str(error))


def apply(revision):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT/'background-upgrade.lock').open('a') as upgrade_lock:
        try:
            fcntl.flock(upgrade_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('An upgrade is already running.', flush=True)
            return
        config = json.loads(CONFIG.read_text())
        with tempfile.TemporaryDirectory(prefix='background-stage-', dir=str(ROOT)) as temp:
            stage = Path(temp)
            for name in FILES:
                download('https://raw.githubusercontent.com/pppaaasss/-/' + revision + '/router/ac86u/' + name, stage/name)
                if name.endswith('.py'):
                    compile((stage/name).read_text(), name, 'exec')
            download(ACK_URL, stage/'activation-report.json')
            # Validate activation before pausing anything; use the actual already
            # acknowledged full report, not a newer partial candidate batch.
            sys.path.insert(0, str(stage))
            sys.path.insert(1, str(BASE))
            from activate import set_actionable
            preview = stage/'preview-config.json'
            atomic_json(preview, config)
            set_actionable(preview, enabled=True, report_path=stage/'activation-report.json')
            backup = ROOT/('before-background-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
            backup.mkdir()
            shutil.copy2(CONFIG, backup/'config.json')
            for name in FILES:
                if (BASE/name).exists():
                    shutil.copy2(BASE/name, backup/name)
            marker = ROOT/'background-upgrade.locked'
            marker.write_text(revision+'\n')
            # This small wrapper also makes scheduled starts wait for the upgrade.
            shutil.copy2(stage/'run.sh', BASE/'run.sh')
            os.chmod(BASE/'run.sh', 0o755)
            config['daily_worker_enabled'] = False
            atomic_json(CONFIG, config)
            print('Update staged. Waiting for the active batch to save its progress.', flush=True)
            try:
                with (ROOT/'daily-worker.lock').open('a') as worker_lock:
                    # The old worker observes daily_worker_enabled after the child
                    # finishes; no kill, lost batch or competing state writer.
                    fcntl.flock(worker_lock, fcntl.LOCK_EX)
                    for name in FILES:
                        target = BASE/(name+'.background-new')
                        shutil.copy2(stage/name, target)
                        os.chmod(target, 0o755)
                        target.replace(BASE/name)
                    ack = ROOT/'activation-report.json'
                    shutil.copy2(stage/'activation-report.json', ack)
                    # Re-read configuration so a concurrent intentional edit is
                    # preserved, and validate the acknowledged report again.
                    config = set_actionable(CONFIG, enabled=True, report_path=ack)
                    from candidate_history import prepare_history
                    state_path = ROOT/'state.json'
                    state = json.loads(state_path.read_text()) if state_path.exists() else {}
                    shutil.copy2(state_path, backup/'state.json') if state_path.exists() else None
                    before = len(state.get('candidate_queue') or [])
                    state = prepare_history(state, ROOT, config['probe_id'], time.time())
                    atomic_json(state_path, state)
                    for path in (ROOT/'daily-jobs').glob('*.json'):
                        job = json.loads(path.read_text())
                        if job.get('kind') == 'primary-0200' and job.get('state') != 'COMPLETE':
                            job.update(phase='final', state='PENDING', retry_after=0)
                            atomic_json(path, job)
                    config['daily_worker_enabled'] = True
                    atomic_json(CONFIG, config)
                    print('History retained:', len(state['tested_candidate_ids']),
                          'duplicates removed:', before-len(state['candidate_queue']),
                          'new addresses remaining:', len(state['candidate_queue']), flush=True)
                    marker.unlink()
            except Exception:
                for name in FILES:
                    if (backup/name).exists():
                        shutil.copy2(backup/name, BASE/name)
                    elif (BASE/name).exists():
                        (BASE/name).unlink()
                shutil.copy2(backup/'config.json', CONFIG)
                marker.unlink(missing_ok=True)
                raise
    env = {k:v for k,v in os.environ.items() if k not in ('LD_LIBRARY_PATH','LD_PRELOAD','PYTHONHOME','PYTHONPATH')}
    with Path('/opt/var/log/iptv-home-probe.log').open('ab') as log:
        subprocess.Popen(['/bin/sh', str(BASE/'run.sh'), '--resume'],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env=env)
    print('BACKGROUND_READY: automatic publishing enabled; candidate discovery continues in the background.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--launch')
    parser.add_argument('--apply')
    args = parser.parse_args()
    revision = args.launch or args.apply or ''
    if not re.fullmatch('[0-9a-f]{40}', revision):
        parser.error('a pinned commit SHA is required')
    if args.launch:
        ROOT.mkdir(parents=True, exist_ok=True)
        env = {k:v for k,v in os.environ.items() if k not in ('LD_LIBRARY_PATH','LD_PRELOAD','PYTHONHOME','PYTHONPATH')}
        with (ROOT/'background-upgrade.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-I', '-X', 'utf8', '-u', str(Path(__file__).resolve()), '--apply', revision],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, env=env)
        print('后台更新已启动，可以关闭 Termux；当前批次保存后自动接续，不需要重新测整份清单。')
    else:
        try:
            apply(revision)
        except Exception as exc:
            print('BACKGROUND_UPDATE_FAILED:', type(exc).__name__, str(exc), flush=True)
            raise


if __name__ == '__main__':
    main()
