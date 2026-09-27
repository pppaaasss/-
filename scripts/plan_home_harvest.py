#!/usr/bin/env python3
"""Fetch upstream text only when fewer than 800 schedulable candidates remain."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router.ac86u.home_contract import make_candidate, validate_candidate_manifest
from scripts.home_thin_control import STATE_SCHEMA, allowed
from scripts.publish_home_decisions import sha256_bytes, unique_core_routes


def plan_harvest(state, manifest, formal_bytes, feedback, probe_id, minimum_pending=800):
    if type(minimum_pending) is not int or minimum_pending < 1:
        raise ValueError('minimum_pending must be a positive integer')
    # Without migrated durable history, never use a raw count to skip discovery.
    if state is None:
        return dict(harvest=True, pending=0, minimum_pending=minimum_pending,
                    reason='history_unavailable')
    if state.get('schema') != STATE_SCHEMA or state.get('probe_id') != probe_id:
        raise ValueError('wrong cloud state identity')
    if state.get('migration_complete') is not True:
        return dict(harvest=True, pending=0, minimum_pending=minimum_pending,
                    reason='history_not_migrated')

    queued = {}
    for identity, raw in state['queue'].items():
        row = make_candidate(raw)
        if row['candidate_id'] != identity:
            raise ValueError('queued candidate identity mismatch')
        queued[identity] = row
    if manifest is not None:
        validate_candidate_manifest(manifest)
        # Count deliverable, not-yet-acknowledged candidates once as well.
        if manifest['formal_playlist']['sha256'] == sha256_bytes(formal_bytes):
            for row in manifest['candidates']:
                queued[row['candidate_id']] = row

    current = unique_core_routes(formal_bytes)
    check = allowed(dict(state, queue=queued), current, feedback)
    pending = sum(identity not in state['tested'] and check(row)
                  for identity, row in queued.items())
    return dict(harvest=pending < minimum_pending, pending=pending,
                minimum_pending=minimum_pending,
                reason='below_minimum' if pending < minimum_pending else 'queue_ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--manifest', default='harvest/home-candidates.json')
    parser.add_argument('--formal', default='tv-core.m3u')
    parser.add_argument('--feedback', default='config/home-route-feedback.json')
    parser.add_argument('--config', default='config/home-thin.json')
    parser.add_argument('--minimum-pending', type=int, default=800)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    if config.get('enabled') is not True:
        state = None
    manifest = json.loads(Path(args.manifest).read_text())
    plan = plan_harvest(state, manifest, Path(args.formal).read_bytes(),
                        Path(args.feedback), config['probe_id'], args.minimum_pending)
    print('HOME_HARVEST_PLAN ' + json.dumps(plan, sort_keys=True))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f"harvest={str(plan['harvest']).lower()}\npending={plan['pending']}\n")
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            action = 'Fetch upstream text once' if plan['harvest'] else 'Use existing queue'
            stream.write(f"Pending untested candidates: **{plan['pending']}**; "
                         f"threshold: **{plan['minimum_pending']}**. {action}.\n")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
