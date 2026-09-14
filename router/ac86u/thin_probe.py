#!/opt/bin/python3
"""Bounded household measurements; no Git, candidate history or decisions."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

try:
    from .thin_contract import (RESULT_SCHEMA, MAX_ENVELOPE, ROUTE_CONTEXT, encode,
        timestamp, epoch, slot, validate_task, validate_observations)
    from .thin_api import GithubData, read_token
except ImportError:
    from thin_contract import (RESULT_SCHEMA, MAX_ENVELOPE, ROUTE_CONTEXT, encode,
        timestamp, epoch, slot, validate_task, validate_observations)
    from thin_api import GithubData, read_token


def read_small(path, default=None):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_ENVELOPE + 1)
    except FileNotFoundError:
        return {} if default is None else default
    if len(raw) > MAX_ENVELOPE:
        raise RuntimeError('local thin state too large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError('invalid local thin state')
    return value


def save(path, value):
    raw = encode(value)
    if len(raw) > MAX_ENVELOPE:
        raise RuntimeError('thin state exceeds bound')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_memory():
    """Own RSS plus descendants only; never inspect or stop unrelated services."""
    pending, seen, total, children = [os.getpid()], set(), 0, []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            fields = dict(line.split(':', 1) for line in Path('/proc/' + str(pid) + '/status').read_text().splitlines() if ':' in line)
            total += int(fields.get('VmRSS', '0').split()[0])
            descendants = Path('/proc/' + str(pid) + '/task/' + str(pid) + '/children').read_text().split()
            pending.extend(int(value) for value in descendants)
            if pid != os.getpid() and fields.get('Name', '').strip() == 'ffprobe':
                children.append(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total, children


def available_memory():
    fields = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return int(fields.get('MemAvailable', '0').split()[0])


class ResourceWatch:
    def __init__(self, config, deadline):
        self.config, self.deadline = config, deadline
        self.reason, self.peak, self.minimum = '', 0, available_memory()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def watch(self):
        while not self.done.is_set():
            try:
                memory, children = process_memory()
                available = available_memory()
                self.peak, self.minimum = max(self.peak, memory), min(self.minimum, available)
                if available < max(65536, int(self.config.get('minimum_available_kib', 65536))):
                    self.reason = self.reason or 'memory_reserve'
                if memory > min(65536, int(self.config.get('maximum_probe_rss_kib', 65536))):
                    self.reason = self.reason or 'probe_rss_budget'
                if time.monotonic() >= self.deadline:
                    self.reason = self.reason or 'batch_time_budget'
                if self.reason:
                    # Only a live FFprobe still descended from this exact worker.
                    for pid in children:
                        if pid in process_memory()[1]:
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
            except (OSError, ValueError):
                self.reason = self.reason or 'resource_sample_unavailable'
            self.done.wait(0.5)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join(timeout=2)


def reserve(ledger, task, config, now):
    day = slot(now)[1]
    ledger = dict(ledger) if ledger.get('day') == day else {'day': day, 'bytes': 0, 'seconds': 0, 'candidates': 0, 'discovery_bytes': 0, 'discovery_seconds': 0}
    candidates = sum(row['role'] == 'candidate' for row in task['tasks'])
    amount, seconds = int(task['limits']['bytes']), int(task['limits']['seconds'])
    if ledger['bytes'] + amount > min(1536 * 1024 * 1024, config['daily_bytes']):
        return ledger, 'daily_byte_budget'
    if ledger['seconds'] + seconds > min(3600, config['daily_seconds']):
        return ledger, 'daily_time_budget'
    if ledger['candidates'] + candidates > min(10, config['daily_candidates']):
        return ledger, 'daily_candidate_budget'
    if candidates and (ledger['discovery_bytes'] + amount > min(256 * 1024 * 1024, config['discovery_bytes']) or ledger['discovery_seconds'] + seconds > min(600, config['discovery_seconds'])):
        return ledger, 'discovery_budget'
    ledger.update(bytes=ledger['bytes'] + amount, seconds=ledger['seconds'] + seconds,
                  candidates=ledger['candidates'] + candidates)
    if candidates:
        ledger['discovery_bytes'] += amount
        ledger['discovery_seconds'] += seconds
    ledger['reservation'] = {'batch_id': task['batch_id'], 'bytes': amount, 'seconds': seconds,
                             'candidates': candidates}
    return ledger, ''


def settle(ledger, result):
    ledger = dict(ledger)
    reserved = ledger.get('reservation', {})
    if reserved.get('batch_id') != result['batch_id']:
        return ledger
    # An interrupted process may have downloaded uncheckpointed bytes: keep
    # its full reservation. Normal completion refunds exactly once.
    if result.get('stop_reason') != 'interrupted_batch':
        refund_bytes = max(0, reserved['bytes'] - result['usage']['downloaded_bytes'])
        refund_seconds = max(0, reserved['seconds'] - result['usage']['runtime_s'])
        ledger['bytes'] -= refund_bytes
        ledger['seconds'] -= refund_seconds
        if reserved['candidates']:
            ledger['discovery_bytes'] -= refund_bytes
            ledger['discovery_seconds'] -= refund_seconds
    ledger.pop('reservation', None)
    return ledger


def measure(task, config, legacy, root):
    # Import media/route helpers only after resource admission. No _run/history.
    try:
        from . import home_probe
        from .home_transport import HomeTransport, DownloadBudget
    except ImportError:
        import home_probe
        from home_transport import HomeTransport, DownloadBudget
    begin, started = time.time(), time.monotonic()
    deadline = started + min(task['limits']['seconds'], max(0, epoch(task['expires_utc']) - begin))
    result = {'schema': RESULT_SCHEMA, 'probe_id': task['probe_id'], 'batch_id': task['batch_id'],
              'cycle_id': task['cycle_id'], 'route_context': ROUTE_CONTEXT,
              'started_utc': timestamp(begin), 'finished_utc': timestamp(begin), 'results': [],
              'usage': {'downloaded_bytes': 0, 'runtime_s': 0}, 'stop_reason': ''}
    save(root / 'working.json', result)
    with ResourceWatch(config, deadline) as watch:
        budget = DownloadBudget(task['limits']['bytes'], deadline, lambda: watch.reason)
        with HomeTransport(legacy.get('lan_dns_server', '192.168.50.1'), download_budget=budget) as transport:
            if home_probe._active_transport is not None:
                raise RuntimeError('household transport already active')
            home_probe._active_transport = transport
            try:
                for row in task['tasks']:
                    if watch.reason or budget.reason or deadline - time.monotonic() < 60:
                        result['stop_reason'] = watch.reason or budget.reason or 'batch_time_budget'
                        break
                    route_start = time.time()
                    settings = dict(legacy, minimum_headroom_ratio=1.05,
                                    viewer_accepted_quality=row.get('viewer_accepted_quality', False))
                    raw = home_probe.probe_route(row['name'], row['url'], floor=row['min_height'],
                        config=settings, sample_limit=row['sample_bytes'], include_metadata=True)
                    raw['channel_key'] = row['channel_key']
                    if watch.reason or budget.reason:
                        # An interrupted route remains queued, including candidates.
                        # Do not permanently mark a locally interrupted URL as tested.
                        result['stop_reason'] = watch.reason or budget.reason
                        break
                    ended = time.time()
                    if ended > epoch(task['expires_utc']):
                        result['stop_reason'] = 'window_closed'
                        break
                    result['results'].append({'task_id': row['task_id'], 'started_utc': timestamp(route_start),
                        'finished_utc': timestamp(ended), 'result': raw})
                    result['finished_utc'] = timestamp(ended)
                    result['usage'] = {'downloaded_bytes': budget.used,
                        'runtime_s': round(time.monotonic() - started, 3),
                        'peak_rss_kib': watch.peak, 'minimum_available_kib': watch.minimum}
                    # At most four small checkpoints; a crash retains completed samples.
                    save(root / 'working.json', result)
            finally:
                home_probe._active_transport = None
        result['usage'] = {'downloaded_bytes': budget.used, 'runtime_s': round(time.monotonic() - started, 3),
                          'peak_rss_kib': watch.peak, 'minimum_available_kib': watch.minimum}
        result['stop_reason'] = result['stop_reason'] or watch.reason or budget.reason
    validate_observations(result, task, time.time())
    return result


def run(config_path):
    config = read_small(config_path)
    if config.get('enabled') is not True:
        print('THIN_DISABLED')
        return 0
    root = Path('/opt/var/lib/iptv-home-thin')
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        legacy_root = Path('/opt/var/lib/iptv-home-probe')
        with (legacy_root / 'daily-worker.lock').open('a') as legacy_lock:
            try:
                fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('THIN_WAITING_LEGACY_WORKER')
                return 0
            return wake(config, root)


def wake(config, root):
    api = GithubData(read_token('/opt/etc/iptv-home-thin.token'))
    state = read_small(root / 'status.json')
    def status(name, **extra):
        state.update(state=name, checked_utc=timestamp(time.time()), **extra)
        save(root / 'status.json', state)
        print('THIN_' + name)
    pending = root / 'outbox.json'
    working = root / 'working.json'
    if working.exists() and not pending.exists():
        interrupted = read_small(working)
        interrupted['stop_reason'] = 'interrupted_batch'
        save(pending, interrupted)
    if pending.exists():
        result = read_small(pending)
        save(root / 'budget.json', settle(read_small(root / 'budget.json'), result))
        api.put_immutable('observations/' + config['probe_id'] + '/' + result['batch_id'] + '.json', encode(result))
        # Persist ACK before dropping the durable outbox. Retry cannot remeasure.
        status('UPLOADED', last_batch=result['batch_id'])
        pending.unlink()
        working.unlink(missing_ok=True)
        api.notify()
    if slot(time.time()) is None:
        status('WAITING_WINDOW')
        return 0
    task = api.task(config['probe_id'])
    if not task or task.get('schema') != 'iptv-home-task/v1':
        status('WAITING_CLOUD', reason=(task or {}).get('state', 'no_task'))
        return 0
    if task.get('batch_id') == state.get('last_batch'):
        status('WAITING_CLOUD_ACK')
        return 0
    if slot(time.time()) is None or time.time() >= epoch(task['expires_utc']):
        status('WAITING_WINDOW')
        return 0
    validate_task(task, config['probe_id'], time.time())
    legacy = read_small('/opt/etc/iptv-home-probe.json')
    if legacy.get('route_context') != ROUTE_CONTEXT or legacy.get('runtime_transport') != 'merlinclash-marked' or legacy.get('protected_publishing_ready') is not True:
        raise RuntimeError('household path or protected publisher not ready')
    if legacy.get('daily_worker_enabled') is not False:
        raise RuntimeError('legacy worker must be disabled before thin mode')
    try:
        from .home_resources import sample_resources
    except ImportError:
        from home_resources import sample_resources
    reason, sample = sample_resources({'minimum_mem_available_kib': max(98304, config['minimum_start_kib'])},
        lambda: {'mem_available_kib': available_memory()})
    if not reason and sample.get('cpu_busy_percent', 100) >= min(75, config['maximum_cpu_percent']):
        reason = 'cpu_busy'
    if reason:
        status('WAITING_RESOURCES', reason=reason, resources=sample)
        heartbeat(api, config, task, root, state, reason)
        return 0
    ledger_path = root / 'budget.json'
    ledger, reason = reserve(read_small(ledger_path), task, config, time.time())
    if reason:
        status('WAITING_BUDGET', reason=reason)
        heartbeat(api, config, task, root, state, reason)
        return 0
    # Crash-safe reservation: abandoned work spends its full lease, never resets budget.
    save(ledger_path, ledger)
    status('RUNNING', batch=task['batch_id'])
    result = measure(task, config, legacy, root)
    save(pending, result)
    save(ledger_path, settle(ledger, result))
    api.put_immutable('observations/' + config['probe_id'] + '/' + result['batch_id'] + '.json', encode(result))
    # Actual usage refunds are optional; reservation accounting is deliberately conservative.
    status('UPLOADED', last_batch=result['batch_id'], usage=result['usage'], reason=result['stop_reason'])
    pending.unlink()
    working.unlink(missing_ok=True)
    api.notify()
    return 0


def heartbeat(api, config, task, root, state, reason):
    if time.time() >= epoch(task['expires_utc']):
        return
    stamp = timestamp(time.time())
    result = {'schema': RESULT_SCHEMA, 'probe_id': config['probe_id'], 'batch_id': task['batch_id'],
              'cycle_id': task['cycle_id'], 'route_context': ROUTE_CONTEXT, 'started_utc': stamp,
              'finished_utc': stamp, 'results': [], 'usage': {'downloaded_bytes': 0, 'runtime_s': 0},
              'stop_reason': reason}
    save(root / 'outbox.json', result)
    api.put_immutable('observations/' + config['probe_id'] + '/' + task['batch_id'] + '.json', encode(result))
    state['last_batch'] = task['batch_id']
    save(root / 'status.json', state)
    (root / 'outbox.json').unlink()
    api.notify()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/etc/iptv-home-thin.json')
    args = parser.parse_args()
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
    try:
        return run(args.config)
    except Exception as exc:
        # Short error state is visible without importing Python in status.sh.
        root = Path('/opt/var/lib/iptv-home-thin')
        if root.exists():
            save(root / 'error.json', {'state': 'STOPPED_ERROR', 'type': type(exc).__name__, 'time': timestamp(time.time())})
        print('THIN_STOPPED_ERROR:', type(exc).__name__, str(exc)[:180], file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
