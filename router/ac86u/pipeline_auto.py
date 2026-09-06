#!/opt/bin/python3
"""Continue isolated trial batches until their saved candidate queue is empty."""
import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path


def next_action(report, previous_remaining, stalled):
    summary = report['summary']
    remaining = int(summary['candidate_queue_remaining'])
    if report.get('resources', {}).get('stop_reason'):
        return 'STOPPED_RESOURCES', remaining, stalled
    if summary['circuit_breaker_open']:
        return 'STOPPED_NETWORK', remaining, stalled
    if report['policy']['candidate_manifest_state'] != 'accepted':
        return 'STOPPED_MANIFEST', remaining, stalled
    if remaining == 0:
        return 'COMPLETE', remaining, 0
    stalled = stalled + 1 if previous_remaining is not None and remaining >= previous_remaining else 0
    return ('STOPPED_NO_PROGRESS' if stalled >= 3 else 'CONTINUING'), remaining, stalled


def run_batches(config_path, *, runner=subprocess.run, sleep=time.sleep):
    config_path = Path(config_path)
    original = config_path.read_bytes()
    config = json.loads(original)
    root = Path(config.get('output_dir') or '/opt/var/lib/iptv-home-probe') / 'pipeline-trial'
    root.mkdir(parents=True, exist_ok=True)
    status_path = root / 'auto-status.json'
    stop_path = root / 'auto-stop'
    rounds, remaining, stalled = 0, None, 0

    def status(state, **extra):
        value = dict(state=state, rounds=rounds, candidate_queue_remaining=remaining,
                     updated_epoch=time.time(), **extra)
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(status_path)
        print('AUTO_STATUS: ' + json.dumps(value), flush=True)

    with (root / 'auto.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('AUTO_ALREADY_RUNNING', flush=True)
            return 1
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
            result = runner([sys.executable, '-u', str(Path(__file__).with_name('pipeline_trial.py')),
                             '--config', str(config_path)])
            if result.returncode == 1:
                # An already running manual batch owns run.lock; wait for it.
                status('WAITING_FOR_RUNNING_BATCH')
                sleep(30)
                continue
            if result.returncode:
                status('STOPPED_BATCH_ERROR', exit_code=result.returncode)
                return 2
            rounds += 1
            try:
                report = json.loads((root / 'latest.json').read_text())
                state, remaining, stalled = next_action(report, remaining, stalled)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                status('STOPPED_REPORT_ERROR', error=type(exc).__name__)
                return 2
            status(state)
            if state != 'CONTINUING':
                return 0 if state == 'COMPLETE' else 2
            sleep(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/etc/iptv-home-probe.json')
    args = parser.parse_args()
    try:
        return run_batches(args.config)
    except Exception as exc:
        print('AUTO_FAILED: ' + type(exc).__name__ + ' ' + str(exc)[:250], flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
