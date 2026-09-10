#!/opt/bin/python3
"""Durable, serialized home jobs. Batch limits never expire a daily job."""
import argparse
import hashlib
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

try:
    from .home_resources import sample_resources
    from .peak_policy import in_peak
except ImportError:
    from home_resources import sample_resources
    from peak_policy import in_peak

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + str(os.getpid()) + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def resource_check(config):
    # Keep the resident supervisor free of the probe's HTTP/media imports.
    def read_resources():
        fields = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        return dict(mem_available_kib=int(fields.get('MemAvailable', '0').split()[0]))
    return sample_resources(config, read_resources)


def maintain_runtime(root, epoch):
    # Only old COMPLETE jobs can be pruned. Outboxes, evidence and diagnostics
    # deliberately have no generic age-based deletion.
    for path in (root / 'daily-jobs').glob('*.json'):
        job = json.loads(path.read_text())
        if job.get('state') == 'COMPLETE' and epoch - job.get('completed', epoch) > 14 * 86400:
            path.unlink()
    log = Path('/opt/var/log/iptv-home-probe.log')
    if log.exists() and log.stat().st_size > 1048576:
        # Copy/truncate preserves inherited descriptors of the active worker.
        with log.open('rb') as source:
            source.seek(max(0, log.stat().st_size - 1048576))
            log.with_suffix('.log.1').write_bytes(source.read())
        with log.open('w'):
            pass


KINDS = ('primary-0200', 'recheck-1300', 'peak-2000')
ZONE = timezone(timedelta(hours=8))


def primary_window(epoch, root=None):
    """Beijing 02:00 inclusive to 08:00 exclusive, independent of host timezone."""
    local = datetime.fromtimestamp(epoch, ZONE)
    start = local.replace(hour=2, minute=0, second=0, microsecond=0)
    end = start.replace(hour=8)
    opened = start <= local < end
    next_start = start if local < start else start + timedelta(days=1)
    if root is not None:
        try:
            manual = json.loads((root / 'manual-primary-window.json').read_text())
            since, until = float(manual['start']), float(manual['until'])
            if since <= epoch < until and 0 < until - since <= 86400:
                return True, until, next_start.timestamp()
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return opened, end.timestamp(), next_start.timestamp()


def request_start_now(root, epoch):
    """Open one temporary window until the next Beijing 08:00; retain progress."""
    local = datetime.fromtimestamp(epoch, ZONE)
    end = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    directory = root / 'daily-jobs'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'enqueue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        unfinished = any(
            job.get('kind') == KINDS[0] and job.get('state') != 'COMPLETE'
            for job in (json.loads(p.read_text()) for p in directory.glob('*.json')))
        if not unfinished:
            ident = local.strftime('%Y%m%d') + '-primary-0200-manual-' + str(int(epoch))
            atomic_json(directory / (ident + '.json'), dict(id=ident, kind=KINDS[0],
                phase='candidates', state='PENDING', created=epoch, retry_after=0, batches=0))
        atomic_json(root / 'manual-primary-window.json', dict(start=epoch, until=end.timestamp()))
    return end


def defer_primary_jobs(root, epoch):
    opened, _end, resume = primary_window(epoch, root)
    if opened:
        return
    waiting = []
    for path in (root / 'daily-jobs').glob('*.json'):
        job = json.loads(path.read_text())
        if job.get('kind') != KINDS[0] or job.get('state') == 'COMPLETE':
            continue
        if job.get('state') != 'WAITING_WINDOW' or job.get('retry_after') != resume:
            job.update(state='WAITING_WINDOW', retry_after=resume)
            atomic_json(path, job)
        waiting.append(job)
    if waiting:
        job = min(waiting, key=lambda row: row['created'])
        atomic_json(root / 'daily-status.json', dict(job,
            reason='primary_window_0200_0800', resume_at=resume))


def enqueue(root, kind, epoch):
    day = datetime.fromtimestamp(epoch, ZONE).strftime('%Y%m%d')
    directory = root / 'daily-jobs'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (day + '-' + kind + '.json')
    # flock serializes publication so the worker never reads a partial request.
    with (directory / 'enqueue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            atomic_json(path, dict(id=path.stem, kind=kind, phase='final' if kind == KINDS[0] else 'full',
                                  state='PENDING', created=epoch, retry_after=0, batches=0))
    return path


def next_job(root, epoch):
    jobs = []
    for path in (root / 'daily-jobs').glob('*.json'):
        job = json.loads(path.read_text())
        window_open = primary_window(epoch, root)[0]
        waiting_for_window = job['kind'] == KINDS[0] and job['state'] == 'WAITING_WINDOW' and window_open
        if job['state'] == 'COMPLETE' or (job.get('retry_after', 0) > epoch and not waiting_for_window):
            continue
        if job['kind'] == KINDS[0] and not window_open:
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
    stop = (report.get('resources') or {}).get('stop_reason')
    if stop and stop != 'primary_window_closed':
        return dict(job, state='WAITING_RESOURCES', reason=stop, retry_after=epoch + 300), False
    if summary.get('circuit_breaker_open'):
        return dict(job, state='WAITING_NETWORK', retry_after=epoch + 300), False
    if job['phase'] == 'candidates':
        job['candidate_manifest_checked'] = True
        if str(policy.get('candidate_manifest_state', '')).startswith('rejected:'):
            return dict(job, state='WAITING_MANIFEST', retry_after=epoch + 300), False
        job['phase'] = 'final'
        return dict(job, state='PENDING'), False
    if policy.get('batch_complete') is True:
        if job['kind'] == KINDS[0] and (summary.get('candidate_queue_remaining', 0) > 0
                or not job.get('candidate_manifest_checked')):
            return dict(job, phase='candidates', state='PENDING'), True
        return dict(job, state='COMPLETE', completed=epoch), True
    return dict(job, state='PENDING'), False


def poll_delivery(config, root, epoch):
    """One text check per wake; an empty/received batch starts no probe."""
    status_path = root / 'delivery-status.json'
    if status_path.exists():
        last = json.loads(status_path.read_text())
        if epoch - last.get('checked_epoch', 0) < 300:
            return
    try:
        # Parsing delivery/history in a short-lived process releases its heap
        # before the media probe starts, including allocator-retained pages.
        result = subprocess.run([sys.executable, '-E', '-s',
            str(Path(__file__).resolve().with_name('candidate_delivery.py'))],
            input=json.dumps(dict(config=config, root=str(root), epoch=epoch)),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, env=clean_env())
        if result.returncode:
            raise RuntimeError('delivery_process_failed:' + result.stderr[-220:])
        pending = json.loads(result.stdout)['pending']
        atomic_json(status_path, dict(state='RECEIVED', checked_epoch=epoch, pending=pending))
    except Exception as exc:
        atomic_json(status_path, dict(state='WAITING_MANIFEST', checked_epoch=epoch,
            reason=type(exc).__name__ + ':' + str(exc)[:220]))
        return
    if not pending:
        return
    directory = root / 'daily-jobs'
    directory.mkdir(parents=True, exist_ok=True)
    if any(json.loads(p.read_text()).get('kind') == KINDS[0]
           and json.loads(p.read_text()).get('state') != 'COMPLETE' for p in directory.glob('*.json')):
        return
    ident = datetime.fromtimestamp(epoch, ZONE).strftime('%Y%m%d') + '-primary-0200-delivery-' + str(int(epoch))
    opened, _, resume = primary_window(epoch, root)
    atomic_json(directory / (ident + '.json'), dict(id=ident, kind=KINDS[0], phase='candidates',
        state='PENDING' if opened else 'WAITING_WINDOW', created=epoch,
        retry_after=0 if opened else resume, batches=0))


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME', 'PYTHONPATH')}


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
        if config.get('daily_worker_enabled') is not True:
            return 0
        poll_delivery(config, root, time.time())
        receipt = root / 'candidate-receipts.json'
        uploaded = root / 'receipt-upload.json'
        uploaded_sha = json.loads(uploaded.read_text()).get('receipt_sha256') if uploaded.exists() else None
        if (config.get('github_push_enabled') and receipt.exists()
                and hashlib.sha256(receipt.read_bytes()).hexdigest() != uploaded_sha):
            subprocess.run([sys.executable, '-E', '-s', str(base / 'push_home_report.py'),
                '--config', str(config_path), '--receipts-only'], env=clean_env())
        while True:
            config = json.loads(config_path.read_text())
            if config.get('daily_worker_enabled') is not True:
                return 0
            now = time.time()
            # Pick up additional user batches while a long-running job is
            # active. poll_delivery already limits text checks to 5 minutes.
            poll_delivery(config, root, now)
            maintain_runtime(root, now)
            defer_primary_jobs(root, now)
            selected = next_job(root, now)
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
                return 0
            atomic_json(root / 'daily-status.json', dict(state='RUNNING', job=job['id'], phase=job['phase']))
            command = [sys.executable, '-E', '-s', '-u', str(base / 'home_probe.py'),
                       '--config', str(config_path), '--run-kind', job['kind'],
                       '--batch-phase', job['phase'], '--batch-cycle-id', job['id']]
            if job['kind'] == KINDS[0]:
                opened, deadline, _resume = primary_window(time.time(), root)
                if not opened:
                    continue
                command.extend(['--stop-at-epoch', str(deadline)])
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
                atomic_json(root / 'daily-status.json', job)
                print('HOME_WORKER: ' + job['state'] + ' retry_after=' + str(job['retry_after']), flush=True)
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
            if job['state'].startswith('WAITING_'):
                print('HOME_WORKER: ' + job['state'] + ' reason=' + str(job.get('reason', '')),
                      flush=True)
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/etc/iptv-home-probe.json')
    parser.add_argument('--enqueue', choices=KINDS)
    parser.add_argument('--start-now', action='store_true', help='Resume discovery now until the next Beijing 08:00')
    parser.add_argument('--detach', action='store_true', help='Launch the worker in the background')
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    root = Path(config['output_dir'])
    if args.start_now:
        if config.get('daily_worker_enabled') is not True:
            raise RuntimeError('daily_worker_disabled')
        if (root / 'background-upgrade.locked').exists() or (root / 'daily-runtime-error.json').exists():
            raise RuntimeError('worker_update_or_runtime_error_pending')
        end = request_start_now(root, time.time())
        print('临时测源窗口已开启，截止北京时间 ' + end.strftime('%m-%d %H:%M') + '；原定时安排保留。', flush=True)
    if args.enqueue:
        enqueue(Path(config['output_dir']), args.enqueue, time.time())
    if args.detach:
        log_path = Path('/opt/var/log/iptv-home-probe.log')
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a') as log:
            proc = subprocess.Popen([sys.executable, '-E', '-s', '-u', str(Path(__file__).resolve()),
                '--config', str(config_path)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                env=clean_env(), start_new_session=True)
        print('后台启动请求已提交，PID=' + str(proc.pid) + '；如已有任务运行，将由同一任务锁串行接续。', flush=True)
        return 0
    return work(config_path)


if __name__ == '__main__':
    raise SystemExit(main())
