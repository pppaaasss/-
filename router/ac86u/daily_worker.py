#!/opt/bin/python3
"""Durable, serialized home jobs. Batch limits never expire a daily job."""
import argparse
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

try:
    from .home_probe import atomic_json, resource_check
    from .peak_policy import in_peak
    from .home_contract import make_candidate, validate_backup_pool
except ImportError:
    from home_probe import atomic_json, resource_check
    from peak_policy import in_peak
    from home_contract import make_candidate, validate_backup_pool

KINDS = ('primary-0200', 'recheck-1300', 'peak-2000')
ZONE = timezone(timedelta(hours=8))


def enqueue(root, kind, epoch):
    day = datetime.fromtimestamp(epoch, ZONE).strftime('%Y%m%d')
    directory = root / 'daily-jobs'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (day + '-' + kind + '.json')
    # flock serializes publication so the worker never reads a partial request.
    with (directory / 'enqueue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            atomic_json(path, dict(id=path.stem, kind=kind, phase='candidates' if kind == KINDS[0] else 'full',
                                  state='PENDING', created=epoch, retry_after=0, batches=0))
    return path


def next_job(root, epoch):
    jobs = []
    for path in (root / 'daily-jobs').glob('*.json'):
        job = json.loads(path.read_text())
        if job['state'] == 'COMPLETE' or job.get('retry_after', 0) > epoch:
            continue
        if job['kind'] == 'peak-2000' and not in_peak(epoch):
            continue
        priority = {'peak-2000': 0, 'recheck-1300': 1, 'primary-0200': 2}[job['kind']]
        jobs.append((priority, job['created'], path, job))
    if not jobs:
        return None
    _, _, path, job = min(jobs, key=lambda row: row[:2])
    return path, job


def advance(job, report, epoch):
    job = dict(job, batches=job['batches'] + 1, retry_after=0)
    policy = report.get('policy') or {}
    summary = report['summary']
    if summary.get('circuit_breaker_open'):
        return dict(job, state='WAITING_NETWORK', retry_after=epoch + 300), False
    if job['phase'] == 'candidates':
        if str(policy.get('candidate_manifest_state', '')).startswith('rejected:'):
            return dict(job, state='WAITING_MANIFEST', retry_after=epoch + 300), False
        if summary['candidate_queue_remaining'] == 0:
            job['phase'] = 'final'
        return dict(job, state='PENDING'), False
    if policy.get('batch_complete') is True:
        return dict(job, state='COMPLETE', completed=epoch), True
    return dict(job, state='PENDING'), False


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME', 'PYTHONPATH')}


def import_trial_candidates(root, config):
    source = root / 'pipeline-trial/qualified-backups.json'
    if not source.exists():
        return
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get('trial_candidate_import_sha256'):
        return
    raw = source.read_bytes()
    pool = json.loads(raw)
    validate_backup_pool(pool, expected_probe_id=config['probe_id'], now_epoch=time.time(),
                         allow_expired=True, trial=True)
    queue = {c['candidate_id']: c for c in state.get('candidate_queue', [])}
    for backup in pool['backups']:
        candidate = make_candidate(dict(name=backup['name'], url=backup['url'],
            request_options=backup.get('request_options', ''), sources=['saved-home-trial']))
        candidate['source_manifest_sha256'] = backup['source_manifest_sha256']
        queue.setdefault(candidate['candidate_id'], candidate)
    state['candidate_queue'] = list(queue.values())
    state['trial_candidate_import_sha256'] = hashlib.sha256(raw).hexdigest()
    atomic_json(state_path, state)
    print('SAVED_TRIAL_CANDIDATES_IMPORTED:', len(pool['backups']), flush=True)


def work(config_path):
    config = json.loads(config_path.read_text())
    root = Path(config['output_dir'])
    root.mkdir(parents=True, exist_ok=True)
    fatal = root / 'daily-runtime-error.json'
    with (root / 'daily-worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if fatal.exists():
            return 1
        base = Path(__file__).resolve().parent
        if config.get('daily_worker_enabled') is True:
            import_trial_candidates(root, config)
        while True:
            config = json.loads(config_path.read_text())
            if config.get('daily_worker_enabled') is not True:
                return 0
            selected = next_job(root, time.time())
            if selected is None:
                # Retry the durable outbox without queuing a partial latest report.
                pending = sorted((root / 'pending-reports').glob('*.json'))
                if pending and config.get('github_push_enabled') is True:
                    reason, _ = resource_check(dict(config, sample_actual_resources=True,
                        minimum_mem_available_kib=58 * 1024))
                    if not reason:
                        subprocess.run([sys.executable, '-E', '-s', str(base / 'push_home_report.py'),
                            '--config', str(config_path), '--report', str(pending[0])], env=clean_env())
                return 0
            path, job = selected
            resume = dict(config, sample_actual_resources=True,
                          minimum_mem_available_kib=max(58 * 1024, int(config.get('minimum_mem_available_kib') or 0) + 8192))
            reason, sample = resource_check(resume)
            if reason:
                atomic_json(root / 'daily-status.json', dict(state='WAITING_RESOURCES', job=job['id'], reason=reason, resources=sample))
                time.sleep(30)
                continue
            atomic_json(root / 'daily-status.json', dict(state='RUNNING', job=job['id'], phase=job['phase']))
            command = [sys.executable, '-E', '-s', '-u', str(base / 'home_probe.py'),
                       '--config', str(config_path), '--run-kind', job['kind'],
                       '--batch-phase', job['phase'], '--batch-cycle-id', job['id']]
            result = subprocess.run(command, env=clean_env())
            now = time.time()
            if result.returncode == 1 or result.returncode < 0:
                atomic_json(fatal, dict(state='STOPPED_RUNTIME_ERROR', job=job['id'], exit_code=result.returncode, epoch=now))
                subprocess.run(['/bin/sh', str(base / 'runtime_audit.sh'), str(root), 'failure',
                                '/opt/var/log/iptv-home-probe.log'], env=clean_env(), timeout=45)
                return 1
            if result.returncode:
                job.update(state='WAITING_RESOURCES' if result.returncode == 75 else 'WAITING_RETRY', retry_after=now + 300)
                atomic_json(path, job)
                continue
            report = json.loads((root / 'latest.json').read_text())
            job, publish = advance(job, report, now)
            # Queue the completed report before marking the job done. The push
            # implementation retains its own outbox if GitHub is unavailable.
            if publish:
                push = subprocess.run([sys.executable, '-E', '-s', str(base / 'push_home_report.py'),
                                       '--config', str(config_path)], env=clean_env())
                job['push_exit_code'] = push.returncode
            atomic_json(path, job)
            atomic_json(root / 'daily-status.json', job)
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/etc/iptv-home-probe.json')
    parser.add_argument('--enqueue', choices=KINDS)
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    if args.enqueue:
        enqueue(Path(config['output_dir']), args.enqueue, time.time())
    return work(config_path)


if __name__ == '__main__':
    raise SystemExit(main())
