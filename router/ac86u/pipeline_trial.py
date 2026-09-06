#!/opt/bin/python3
"""Run the primary/recheck engine with isolated, non-publishable evidence."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal

try:
    from . import home_probe as probe
except ImportError:
    import home_probe as probe


class TrialStopped(BaseException):
    pass


def stop(_signum, _frame):
    raise TrialStopped('interrupted')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/etc/iptv-home-probe.json')
    parser.add_argument('--run-kind', choices=sorted(probe.RUN_KINDS), default='primary-0200')
    parser.add_argument('--phase', choices=['full', 'candidates', 'final'], default='full')
    args = parser.parse_args()
    original = Path(args.config).read_bytes()
    config = json.loads(original)
    root = Path(config.get('output_dir') or '/opt/var/lib/iptv-home-probe') / 'pipeline-trial'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('ALREADY_RUNNING', flush=True)
            return 1
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, stop)
        os.nice(15)
        config.update(trial_phase=args.phase, runtime_transport='merlinclash-marked', lan_dns_server='192.168.50.1',
                      sample_actual_resources=True, progress_log=True,
                      minimum_mem_available_kib=40 * 1024,
                      minimum_headroom_ratio=1.35, minimum_h264_stream_mbps=3.0, actionable=False, github_push_enabled=False,
                      trial_candidate_file=str(Path(__file__).with_name('home-trial-candidates.json.gz')),
                      candidate_manifest_url='https://raw.githubusercontent.com/pppaaasss/-/home-first-ac86u/harvest/home-trial-candidates.json.gz')
        print('PIPELINE_START: ' + args.run_kind + ' headroom=1.35 h264_min_mbps=3.0 memory_stop_mib=40 memory_resume_mib=48', flush=True)
        print('PIPELINE_PHASE: ' + args.phase, flush=True)
        print('OUTPUT: ' + str(root / 'latest.json'), flush=True)
        try:
            report, state = probe.run(config, run_kind=args.run_kind, trial=True)
            for row in ([] if args.phase == 'candidates' else report['current_results']):
                if row['status'] != 'GOOD':
                    print('CURRENT_ISSUE: ' + json.dumps(dict(name=row['name'], status=row['status'],
                        reason=row['error'], attempts=row['attempt_count']), ensure_ascii=False), flush=True)
            print('PIPELINE_SUMMARY: ' + json.dumps(report['summary'], ensure_ascii=False), flush=True)
            print('CANDIDATE_MANIFEST: ' + state['candidate_manifest_state'], flush=True)
            print('RESOURCES: ' + json.dumps(report['resources']), flush=True)
            print('TRANSPORT: ' + json.dumps(report.get('transport')), flush=True)
            if report['summary']['circuit_breaker_open']:
                stage = 'PAUSED_NETWORK_CIRCUIT'
            elif args.run_kind == 'recheck-1300':
                stage = 'CACHE_ONLY'
            elif report['summary']['candidate_queue_remaining']:
                stage = 'PARTIAL_QUEUE_SAVED'
            else:
                stage = 'QUEUE_FINISHED'
            print('CANDIDATE_STAGE: ' + stage, flush=True)
            print('TRIAL_ONLY: report saved; production disabled', flush=True)
            return 0
        except TrialStopped:
            print('PIPELINE_STOPPED: interrupted; last completed run remains saved', flush=True)
            return 130
        except Exception as exc:
            print('PIPELINE_FAILED: ' + type(exc).__name__ + ' ' + str(exc)[:250], flush=True)
            if isinstance(exc, RuntimeError) and str(exc).startswith('RESOURCE_GUARD:'):
                return 75
            return 2
        finally:
            print('CONFIG_UNCHANGED: ' + str(hashlib.sha256(original).digest() ==
                  hashlib.sha256(Path(args.config).read_bytes()).digest()), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
