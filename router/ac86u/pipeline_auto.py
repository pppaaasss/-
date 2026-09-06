#!/opt/bin/python3
"""Continue isolated trial batches until their saved candidate queue is empty."""
import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path


def recovery_check(config):
    try:
        from .home_resources import sample_resources
    except ImportError:
        from home_resources import sample_resources

    def memory():
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                values['mem_available_kib'] = int(line.split()[1])
                break
        return values

    # Resume with 8 MiB of margin above the unchanged in-batch stop threshold.
    recovery = dict(config, minimum_mem_available_kib=
                    max(64 * 1024, int(config.get('minimum_mem_available_kib') or 0)) + 8 * 1024)
    return sample_resources(recovery, memory)


def next_action(report, previous_remaining, stalled):
    summary = report['summary']
    remaining = int(summary['candidate_queue_remaining'])
    if report.get('resources', {}).get('stop_reason'):
        return 'WAITING_RESOURCES', remaining, 0 if previous_remaining is None or remaining < previous_remaining else stalled
    if summary['circuit_breaker_open']:
        return 'STOPPED_NETWORK', remaining, stalled
    if report['policy']['candidate_manifest_state'] != 'accepted':
        return 'STOPPED_MANIFEST', remaining, stalled
    if remaining == 0:
        return 'COMPLETE', remaining, 0
    stalled = stalled + 1 if previous_remaining is not None and remaining >= previous_remaining else 0
    return ('STOPPED_NO_PROGRESS' if stalled >= 3 else 'CONTINUING'), remaining, stalled


def run_batches(config_path, *, runner=subprocess.run, sleep=time.sleep, check_resources=None, replace_running=False):
    check_resources = recovery_check if check_resources is None else check_resources
    config_path = Path(config_path)
    original = config_path.read_bytes()
    config = json.loads(original)
    root = Path(config.get('output_dir') or '/opt/var/lib/iptv-home-probe') / 'pipeline-trial'
    root.mkdir(parents=True, exist_ok=True)
    status_path = root / 'auto-status.json'
    stop_path = root / 'auto-stop'
    rounds, remaining, stalled = 0, None, 0
    phase, final_incomplete = "candidates", 0

    def status(state, **extra):
        value = dict(state=state, rounds=rounds, candidate_queue_remaining=remaining,
                     phase=phase, updated_epoch=time.time(), **extra)
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(status_path)
        print('AUTO_STATUS: ' + json.dumps(value), flush=True)

    with (root / 'auto.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not replace_running:
                print('AUTO_ALREADY_RUNNING', flush=True)
                return 1
            # One detached handover waiter; let the old batch save its queue
            # and remove its temporary transport rule before taking ownership.
            with (root / 'auto-handover.lock').open('a') as handover:
                try:
                    fcntl.flock(handover, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print('AUTO_HANDOVER_ALREADY_WAITING', flush=True)
                    return 1
                stop_path.touch()
                deadline = time.monotonic() + 1500
                print('WAITING_PREVIOUS_AUTO: finishing its current batch', flush=True)
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            print('AUTO_HANDOVER_TIMEOUT: old controller still owns lock', flush=True)
                            return 2
                        sleep(5)
        # Clear a stop request from a previous, explicitly restarted controller.
        stop_path.unlink(missing_ok=True)
        status('STARTED')
        while True:
            if stop_path.exists():
                status('STOPPED_BY_USER')
                return 0
            if config_path.read_bytes() != original:
                status('STOPPED_CONFIG_CHANGED')
                return 2
            reason, resources = check_resources(config)
            if reason:
                status('WAITING_RESOURCES', reason=reason, resources=resources, retry_seconds=60)
                sleep(60)
                continue
            result = runner([sys.executable, '-u', str(Path(__file__).with_name('pipeline_trial.py')),
                             '--config', str(config_path), '--phase', phase])
            if result.returncode == 1:
                # An already running manual batch owns run.lock; wait for it.
                status('WAITING_FOR_RUNNING_BATCH')
                sleep(30)
                continue
            if result.returncode == 75:
                # Resources can fall between the controller check and child start.
                status('WAITING_RESOURCES', reason='batch_start_resource_guard', retry_seconds=60)
                sleep(60)
                continue
            if result.returncode:
                status('STOPPED_BATCH_ERROR', exit_code=result.returncode)
                return 2
            rounds += 1
            try:
                report = json.loads((root / 'latest.json').read_text())
                if phase == 'final':
                    remaining = int(report['summary']['candidate_queue_remaining'])
                    if report.get('resources', {}).get('stop_reason'):
                        state = 'WAITING_RESOURCES'
                    elif report['summary']['circuit_breaker_open']:
                        state = 'STOPPED_NETWORK'
                    elif remaining:
                        state = 'STOPPED_REPORT_ERROR'
                    elif report['policy'].get('final_review_complete') is True:
                        state = 'COMPLETE'
                    else:
                        final_incomplete += 1
                        state = 'CONTINUING' if final_incomplete < 3 else 'STOPPED_FINAL_INCOMPLETE'
                else:
                    state, remaining, stalled = next_action(report, remaining, stalled)
                    if state == 'COMPLETE':
                        phase = 'final'
                        state = 'CONTINUING'
                        status('FINAL_REVIEW_PENDING')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                status('STOPPED_REPORT_ERROR', error=type(exc).__name__)
                return 2
            status(state)
            if state == 'WAITING_RESOURCES':
                sleep(60)
                continue
            if state != 'CONTINUING':
                return 0 if state == 'COMPLETE' else 2
            sleep(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/etc/iptv-home-probe.json')
    parser.add_argument('--replace-running', action='store_true')
    args = parser.parse_args()
    try:
        return run_batches(args.config, replace_running=args.replace_running)
    except Exception as exc:
        print('AUTO_FAILED: ' + type(exc).__name__ + ' ' + str(exc)[:250], flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
