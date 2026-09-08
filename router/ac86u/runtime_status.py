#!/opt/bin/python3
"""Small local status and explicit recovery; no media probe or environment repair."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time


def read(path):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return {}


def recover(config_path):
    config = read(config_path)
    root = Path(config['output_dir'])
    with (root / 'daily-worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / 'background-upgrade.locked').exists():
            raise RuntimeError('unfinished upgrade: background_upgrade.py --recover first')
        if not config.get('daily_worker_enabled'):
            raise RuntimeError('worker disabled: restore matching upgrade before resuming')
        for path in Path(__file__).resolve().parent.glob('*.py'):
            compile(path.read_text(), str(path), 'exec')
        try:
            from . import home_probe, daily_worker
        except ImportError:
            import home_probe, daily_worker
        # Parse persistent inputs and replay completed evidence before clearing
        # the stop. A corrupt file remains stopped with its original diagnosis.
        for name in ('state.json','qualified-backups.json'):
            read(root / name)
        home_probe.replay_progress(read(root / 'state.json'), root)
        target = root / 'recovery-write-check'
        with target.open('w') as stream:
            stream.write('ok'); stream.flush(); os.fsync(stream.fileno())
        target.unlink()
        fatal = root / 'daily-runtime-error.json'
        if fatal.exists():
            directory = root / 'runtime-diagnostics'; directory.mkdir(exist_ok=True)
            destination = directory / 'first-runtime-error.json'
            if destination.exists(): destination = directory / 'last-runtime-error.json'
            fatal.replace(destination)
    print('RUNTIME_READY: diagnostic retained; next scheduled wake resumes saved work.')


def status(config_path):
    config = read(config_path); root = Path(config['output_dir'])
    try:
        from .daily_worker import primary_window
    except ImportError:
        from daily_worker import primary_window
    version = read(root / 'installed-version.json')
    fatal = read(root / 'daily-runtime-error.json')
    job = read(root / 'daily-status.json')
    delivery = read(root / 'delivery-status.json')
    state = read(root / 'state.json'); report = read(root / 'latest.json')
    upload = read(root / 'github-state.json'); receipt = read(root / 'candidate-receipts.json')
    opened, end, next_start = primary_window(time.time(), root)
    print('version:', version.get('revision', 'unrecorded; inspect installed files'))
    print('worker:', 'enabled' if config.get('daily_worker_enabled') else 'disabled',
          'state:', fatal.get('state') or job.get('state', 'NO_REPORT'))
    if (root / 'background-upgrade.locked').exists(): print('upgrade: INTERRUPTED_OR_RUNNING; inspect marker')
    print('primary_window:', 'open' if opened else 'waiting', 'next_epoch:', end if opened else next_start)
    print('job:', json.dumps(fatal or job, ensure_ascii=False))
    print('delivery:', json.dumps(delivery, ensure_ascii=False), 'received_batches:',len(receipt.get('received_batches',[])))
    print('queue:',len(state.get('candidate_queue',[])), 'tested_ids:',len(state.get('tested_candidate_ids',[])),
          'pending_upload:',len(list((root/'pending-reports').glob('*.json'))))
    print('latest_report:',report.get('generated_utc','NO_REPORT'),report.get('run_kind',''),report.get('summary',{}))
    print('upload:',upload.get('last_push_utc','NONE'), 'error:',upload.get('last_error','none'))
    print('publication: use GitHub home-publish/latest.json; upload success alone is not publication')
    for row in report.get('decisions',[]):
        if row.get('action') == 'UNRESOLVED': print('unresolved:',row['channel_key'],row['reason'])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='/opt/etc/iptv-home-probe.json')
    parser.add_argument('--recover',action='store_true')
    args=parser.parse_args()
    (recover if args.recover else status)(Path(args.config))


if __name__ == '__main__': main()
