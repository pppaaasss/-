#!/usr/bin/env python3
"""GitHub-only history, scheduling and decisions from household observations.

No network/media client is called here. Output uses the existing v2 publisher
contract, with explicit aggregation provenance and the oldest measurement time.
"""
import argparse
import base64
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router.ac86u.thin_contract import (TASK_SCHEMA, RESULT_SCHEMA, ROUTE_CONTEXT,
    MAX_ENVELOPE, KINDS, encode, digest, epoch, timestamp, slot, seal_task,
    validate_observations, validate_task)
from router.ac86u.home_contract import (REPORT_SCHEMA, BACKUP_SCHEMA, candidate_id,
    canonical_name, url_sha256, validate_home_report_v2, validate_candidate_manifest,
    validate_backup_pool, _verification)
from router.ac86u.home_decision import (current_result, candidate_result, candidate_is_qualified,
    mass_failure_circuit, probe_is_good, update_backup_pool, eligible_backups,
    cached_backup_result, backup_score)
from router.ac86u.peak_policy import apply_peak_policy
from router.ac86u.push_home_report import report_filename
from scripts.publish_home_decisions import unique_core_routes, atomic_json, sha256_bytes
from scripts.home_route_policy import rejected_urls, rejected_hosts
from urllib.parse import urlsplit

STATE_SCHEMA = 'iptv-home-cloud-state/v1'


def new_state(probe):
    return {'schema': STATE_SCHEMA, 'probe_id': probe, 'tested': {}, 'archive': {},
            'queue': {}, 'peak_failures': {}, 'completed_slots': [], 'batches': {},
            'processed': {}, 'cycle': None, 'migration_complete': False}


def import_legacy(state, files, migration_id):
    """Cloud parses legacy history once. Old timestamps and vetoes stay intact."""
    if 'state.json' not in files:
        raise ValueError('migration lacks household state.json')
    sources = {}
    for name, raw in files.items():
        if name not in ('state.json', 'qualified-backups.json', 'pipeline-trial/state.json',
                        'pipeline-trial/qualified-backups.json', 'progress.jsonl', 'quality-policy.json'):
            raise ValueError('unexpected migration file')
        if name == 'progress.jsonl':
            # Accept only complete records; preserve a torn tail in the source archive.
            lines = raw.splitlines(keepends=True)
            sources[name] = [json.loads(line) for line in lines if line.endswith(b'\n')]
        else:
            sources[name] = json.loads(raw.decode('utf-8'))
            if not isinstance(sources[name], dict):
                raise ValueError('invalid migration object')
    result = copy.deepcopy(state)
    for name in ('pipeline-trial/state.json', 'state.json'):
        old = sources.get(name, {})
        for identity in old.get('tested_candidate_ids', []):
            result['tested'].setdefault(identity, {'legacy': True})
        for identity, row in old.get('candidate_observations', {}).items():
            result['tested'][identity] = row
        for identity, row in old.get('backup_archive', {}).items():
            result['archive'][identity] = dict(row, requires_home_reverification=True) if name.startswith('pipeline-trial/') else row
        result['peak_failures'].update(old.get('peak_failures', {}))
        for row in old.get('candidate_queue', []):
            result['queue'][row['candidate_id']] = row
    for name in ('pipeline-trial/qualified-backups.json', 'qualified-backups.json'):
        pool = sources.get(name)
        if pool:
            validate_backup_pool(pool, expected_probe_id=state['probe_id'], allow_expired=True,
                                 trial=name.startswith('pipeline-trial/'))
            for row in pool['backups']:
                if name.startswith('pipeline-trial/'):
                    # Formal history may contain later failed rechecks or cooldowns.
                    # Import trial-only entries without overwriting that evidence.
                    formal = sources['state.json'].get('backup_archive', {}).get(row['candidate_id'], {})
                    row = {**row, **formal, 'requires_home_reverification': True}
                result['archive'][row['candidate_id']] = row
                result['tested'].setdefault(row['candidate_id'], {'legacy': True})
    for row in sources.get('progress.jsonl', []):
        if row['kind'] == 'candidate':
            result['tested'][row['identity']] = row['observation']
        if row.get('backup'):
            result['archive'][row['identity']] = row['backup']
    result['quality_policy'] = sources.get('quality-policy.json', {})
    for identity, row in result['archive'].items():
        if identity != candidate_id(row['channel_key'], row['url'], row.get('request_options', '')):
            raise ValueError('legacy backup identity mismatch')
    result['queue'] = {k: v for k, v in result['queue'].items() if k not in result['tested']}
    result.update(migration_complete=True, migration_id=migration_id,
        migration_files={name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
    return result


def read_migration(directory):
    manifest_path = directory / 'manifest.json'
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get('schema') != 'iptv-home-migration/v1' or not 1 <= len(manifest.get('files', [])) <= 6:
        raise ValueError('invalid migration manifest')
    files = {}
    for entry in manifest['files']:
        packed = bytearray()
        parts = entry['parts']
        if not isinstance(parts, list) or not 1 <= len(parts) <= 1024:
            raise ValueError('invalid migration chunks')
        for identity in parts:
            if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
                raise ValueError('invalid migration chunk name')
            path = directory / (identity + '.json')
            if not path.exists():
                return None
            raw = path.read_bytes()
            if len(raw) > MAX_ENVELOPE:
                raise ValueError('migration chunk too large')
            chunk = base64.b64decode(json.loads(raw)['data'], validate=True)
            if hashlib.sha256(chunk).hexdigest() != identity or len(chunk) > 32768:
                raise ValueError('migration chunk hash mismatch')
            packed.extend(chunk)
        # Bounded decompression, including malformed compressed input.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024 or len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('migration file hash or size mismatch')
        if entry['name'] in files:
            raise ValueError('duplicate migration file')
        files[entry['name']] = raw
    return files, digest(manifest)


def normalise(raw, task):
    """Validate numeric evidence and apply the agreed household quality policy."""
    result = dict(raw, channel_key=task['channel_key'], min_height=task['min_height'])
    from router.ac86u.home_decision import probe_verification
    _verification(probe_verification(result), 'thin evidence', require_deep=False)
    if result['observed_status'] in ('GOOD', 'DEGRADED'):
        samples = result.get('segment_samples', [])
        if result.get('sample_count') != 2 or len(samples) != 2:
            raise ValueError('successful measurement lacks two samples')
        for sample in samples:
            from router.ac86u.thin_contract import bounded_number
            bounded_number(sample['downloaded_bytes'], 65536, task['sample_bytes'])
            bounded_number(sample['elapsed_s'], 0.001, 30)
        if result.get('deep_checked') is not True:
            result.update(observed_status='UNKNOWN', error='quality_metadata_unavailable')
        else:
            rate = float(result.get('stream_mbps') or result.get('bitrate_mbps') or 0)
            codec = result.get('codec', '').lower()
            floor = task.get('minimum_hevc_stream_mbps', 2.5) if codec in ('hevc', 'h265') else task.get('minimum_h264_stream_mbps' if codec == 'h264' else 'minimum_other_stream_mbps', 3.0)
            result['headroom_ratio'] = result.get('min_download_mbps', 0) / rate if rate else 0
            quality_bad = result.get('height', 0) < task['min_height'] or rate < floor
            if rate <= 0:
                result.update(observed_status='UNKNOWN', error='intrinsic_stream_bitrate_unknown')
            elif result.get('headroom_ratio', 0) < 1.05 or (quality_bad and not task.get('viewer_accepted_quality')):
                result.update(observed_status='DEGRADED', error='home_quality_threshold')
            else:
                result['observed_status'] = 'GOOD'
    return result


def task_row(cycle, key, url, role, attempt=1):
    identity = candidate_id(key, url)
    policy = cycle.get('quality_policy', {})
    floor = policy.get('minimum_height_overrides', {}).get(canonical_name(key), policy.get('minimum_height_default', 1080))
    return {'task_id': digest([cycle['id'], role, key, url, attempt]), 'role': role,
            'channel_key': key, 'name': canonical_name(key), 'url': url, 'candidate_id': identity,
            'attempt': attempt, 'sample_bytes': (2 if role == 'current' else 6) * 1024 * 1024,
            'metadata': True, 'min_height': floor,
            'viewer_accepted_quality': role == 'current' and url in cycle.get('viewer_good_urls', []),
            **{k: policy[k] for k in ('minimum_h264_stream_mbps', 'minimum_hevc_stream_mbps', 'minimum_other_stream_mbps') if k in policy}}


def allowed(state, current, feedback):
    urls = rejected_urls(feedback)
    hosts = rejected_hosts(feedback, 2)
    identities = {}
    for key, route in current.items():
        identities.setdefault(route.url, set()).add(key)
    for row in list(state['queue'].values()) + list(state['archive'].values()):
        identities.setdefault(row['url'], set()).add(row['channel_key'])
    conflicts = {url for url, keys in identities.items() if len(keys) > 1}
    def check(row):
        return (row['channel_key'] in current and row['url'] != current[row['channel_key']].url
                and row['url'] not in urls | conflicts and (urlsplit(row['url']).hostname or '').lower() not in hosts
                and not row.get('request_options'))
    return check


def current_view(state, cycle):
    attempts = {}
    for key, value in cycle['current'].items():
        attempts[key] = [cycle['results'][task_row(cycle, key, value['url'], 'current', attempt)['task_id']]['result']
            for attempt in (1, 2) if task_row(cycle, key, value['url'], 'current', attempt)['task_id'] in cycle['results']]
    if any(not rows for rows in attempts.values()):
        raise ValueError('incomplete current cycle')
    circuit = mass_failure_circuit(attempts, minimum_channels=12, failure_ratio=0.35)
    rows = [current_result(canonical_name(key), cycle['current'][key]['url'], attempts[key], circuit_open=circuit)
            for key in sorted(attempts)]
    oldest = min(epoch(row['started_utc']) for row in cycle['results'].values())
    rows, peaks = apply_peak_policy(rows, state['peak_failures'], run_kind=cycle['kind'],
                                   now_epoch=oldest, circuit_open=circuit)
    return rows, circuit, peaks


def backup_record(state, task, observation, cycle):
    raw = observation['result']
    identity = task['candidate_id']
    measured = epoch(observation['finished_utc'])
    if candidate_is_qualified(raw):
        candidate = dict(task, request_options='', source_manifest_sha256=cycle['manifest_sha'])
        pool = update_backup_pool(None, [(candidate, raw)], probe_id=state['probe_id'],
            now_epoch=measured, formal_playlist_sha256=cycle['binding']['sha256'],
            candidate_manifest_sha256=cycle['manifest_sha'], current_urls={k:v['url'] for k,v in cycle['current'].items()},
            ttl_hours=36, run_kind=cycle['kind'])
        if pool['backups']:
            state['archive'][identity] = pool['backups'][0]
    elif identity in state['archive']:
        state['archive'][identity].update(last_recheck_result='failed_or_unknown', retry_after_epoch=measured + 3600)
    if task['role'] == 'candidate':
        state['tested'][identity] = {'last_checked_utc': observation['finished_utc'], 'result': raw}
        state['queue'].pop(identity, None)


def ingest(state, reports, now):
    directory = reports / 'observations' / state['probe_id']
    for path in sorted(directory.glob('*.json')):
        if path.stem in state['processed']:
            if hashlib.sha256(path.read_bytes()).hexdigest() != state['processed'][path.stem]:
                raise ValueError('processed observation changed')
            continue
        if path.is_symlink() or path.stat().st_size > MAX_ENVELOPE:
            raise ValueError('unsafe observations file')
        task = state['batches'].get(path.stem)
        if task is None:
            # Unknown batches are not executed or accepted as household evidence.
            continue
        raw = path.read_bytes()
        value = validate_observations(json.loads(raw), task, now)
        if path.stem != value['batch_id']:
            raise ValueError('observation filename mismatch')
        state['processed'][path.stem] = hashlib.sha256(raw).hexdigest()
        budget = state.get('native_budget', {})
        reserved = budget.get('reservations', {}).pop(path.stem, None)
        if reserved and value.get('stop_reason') != 'interrupted_batch':
            refund_bytes = max(0, reserved['bytes'] - value['usage']['downloaded_bytes'])
            refund_seconds = max(0, reserved['seconds'] - value['usage']['runtime_s'])
            budget['bytes'] -= refund_bytes
            budget['seconds'] -= refund_seconds
            if reserved['candidates']:
                budget['discovery_bytes'] -= refund_bytes
                budget['discovery_seconds'] -= refund_seconds
        state['last_heartbeat'] = {'received_utc': timestamp(now), 'measured_utc': value['finished_utc'],
            'stop_reason': value.get('stop_reason', ''), 'usage': value['usage'], 'results': len(value['results'])}
        cycle = state.get('cycle')
        if not cycle or value['cycle_id'] != cycle['id']:
            continue
        if cycle.get('outstanding') == value['batch_id']:
            cycle.pop('outstanding', None)
        requested = {row['task_id']: row for row in task['tasks']}
        for observation in value['results']:
            row = requested[observation['task_id']]
            if row['task_id'] in cycle['results']:
                continue  # A late expired lease cannot overwrite accepted evidence.
            observation = dict(observation, result=normalise(observation['result'], row))
            cycle['results'][row['task_id']] = observation
            cycle['task_rows'][row['task_id']] = row
            if row['role'] != 'current':
                backup_record(state, row, observation, cycle)
        if value.get('stop_reason') in ('daily_candidate_budget', 'discovery_budget'):
            cycle['discovery_closed'] = True
        elif value.get('stop_reason') in ('daily_byte_budget', 'daily_time_budget') and all(row['role'] != 'current' for row in task['tasks']):
            cycle['optional_closed'] = True
        elif not value['results'] and value.get('stop_reason'):
            cycle['retry_after'] = now + 15 * 60


def next_tasks(state, cycle, config, current, feedback, now):
    pending = []
    for key, route in cycle['current'].items():
        first = task_row(cycle, key, route['url'], 'current')
        if first['task_id'] not in cycle['results']:
            pending.append(first)
    if pending:
        return pending[:config['routes_per_batch']]
    for key, route in cycle['current'].items():
        first = task_row(cycle, key, route['url'], 'current')
        second = task_row(cycle, key, route['url'], 'current', 2)
        if not probe_is_good(cycle['results'][first['task_id']]['result']) and second['task_id'] not in cycle['results']:
            pending.append(second)
    if pending:
        return pending[:config['routes_per_batch']]
    rows, circuit, _ = current_view(state, cycle)
    bad = {row['channel_key'] for row in rows if row['status'] == 'BAD'}
    if circuit or cycle['kind'] == KINDS[1] or cycle.get('optional_closed'):
        return []
    check = allowed(state, current, feedback)
    for key in sorted(bad):
        measured_good = any(row['role'] != 'current' and row['channel_key'] == key
            and candidate_is_qualified(cycle['results'][identity]['result']) for identity, row in cycle['task_rows'].items())
        if measured_good:
            continue
        backups = sorted((r for r in state['archive'].values() if r['channel_key'] == key and check(r)), key=lambda r:-backup_score(r))
        for backup in backups:
            task = task_row(cycle, key, backup['url'], 'backup')
            if task['task_id'] not in cycle['results'] and now >= backup.get('retry_after_epoch', 0):
                pending.append(task)
                break
    if pending:
        return pending[:min(2, config['routes_per_batch'])]
    if cycle['kind'] == KINDS[0] and not cycle.get('discovery_closed'):
        count = sum(row['role'] == 'candidate' for row in cycle['task_rows'].values())
        remaining = max(0, min(10, config['daily_candidates']) - count)
        for candidate in sorted(state['queue'].values(), key=lambda r:(r['channel_key'] not in bad, r['candidate_id'])):
            if remaining <= len(pending):
                break
            if candidate['candidate_id'] not in state['tested'] and check(candidate):
                pending.append(task_row(cycle, candidate['channel_key'], candidate['url'], 'candidate'))
        return pending[:min(2, config['routes_per_batch'])]
    return []


def complete_report(state, cycle, current, feedback, now):
    rows, circuit, peaks = current_view(state, cycle)
    state['peak_failures'] = peaks
    bad = {row['channel_key'] for row in rows if row['status'] == 'BAD'}
    check = allowed(state, current, feedback)
    choices, candidates = {}, []
    for identity, task in cycle['task_rows'].items():
        if task['role'] == 'current':
            continue
        raw = cycle['results'][identity]['result']
        switching = task['channel_key'] in bad and check(task) and candidate_is_qualified(raw)
        evidence = candidate_result(dict(task, request_options=''), raw,
            purpose='switch-reverification' if switching else 'daily-qualification', switch_reverified=switching)
        evidence['observed_utc'] = cycle['results'][identity]['finished_utc']
        candidates.append(evidence)
        if switching:
            choices.setdefault(task['channel_key'], task['candidate_id'])
    generated = min(epoch(row['started_utc']) for row in cycle['results'].values())
    if cycle['kind'] == KINDS[1] and not circuit:
        for key in sorted(bad):
            for backup in sorted(state['archive'].values(), key=lambda r:-backup_score(r)):
                if (backup['channel_key'] == key and check(backup) and not backup.get('last_recheck_result')
                        and not backup.get('requires_home_reverification')
                        and backup.get('verified_run_kind') in (KINDS[0], KINDS[2])
                        and epoch(backup.get('expires_utc', '1970-01-01T00:00:00Z')) >= now
                        and epoch(backup['last_verified_utc']) <= generated):
                    candidates.append(cached_backup_result(backup))
                    choices[key] = backup['candidate_id']
                    break
    decisions = []
    for row in rows:
        key = row['channel_key']
        replace = row['status'] == 'BAD' and key in choices and not circuit
        decisions.append({'channel_key': key, 'action': 'REPLACE' if replace else ('KEEP' if row['status'] == 'GOOD' else 'UNRESOLVED'),
                          'reason': 'home_measurements_aggregated_in_cloud',
                          'replacement_candidate_id': choices[key] if replace else None})
    report = {'schema': REPORT_SCHEMA, 'probe_id': state['probe_id'], 'generated_utc': timestamp(generated),
        'run_kind': cycle['kind'], 'run_status': 'COMPLETED', 'production_modified': False,
        'actionable': True, 'route_context': ROUTE_CONTEXT, 'formal_playlist': cycle['binding'],
        'home_feedback_sha256': cycle['feedback_sha'],
        'baseline': {'home_network_ok': not circuit, 'github_reachable': True, 'route_verified': True,
                     'mass_failure_circuit_breaker': circuit},
        'current_results': rows, 'candidate_results': candidates, 'decisions': decisions,
        'policy': {'batch_complete': True, 'formal_check_deferred': False, 'minimum_headroom_ratio': 1.05,
                   'decision_origin': 'cloud_from_household_measurements', 'cloud_stream_probe_performed': False},
        'aggregation': {'cycle_id': cycle['id'], 'oldest_measurement_utc': timestamp(generated),
                        'newest_measurement_utc': max(row['finished_utc'] for row in cycle['results'].values()),
                        'batch_ids': sorted(cycle['batch_ids'])},
        'summary': {'channels': len(rows), 'good': sum(r['status']=='GOOD' for r in rows),
                    'bad': len(bad), 'unknown': sum(r['status']=='UNKNOWN' for r in rows),
                    'replacements': sum(r['action']=='REPLACE' for r in decisions)}}
    validate_home_report_v2(report, expected_probe_id=state['probe_id'], now_epoch=now, max_age_hours=18)
    return report


def step(state, config, root, reports, now):
    if state['schema'] != STATE_SCHEMA or state['probe_id'] != config['probe_id']:
        raise ValueError('wrong cloud state identity')
    state = copy.deepcopy(state)
    active = slot(now)
    idle = {'schema': 'iptv-home-idle/v1', 'probe_id': config['probe_id'], 'state': 'WAITING_WINDOW', 'generated_utc': timestamp(now)}
    if config.get('enabled') is not True:
        return state, dict(idle, state='DISABLED'), None
    if not state['migration_complete']:
        migration = read_migration(reports / 'migrations' / config['probe_id'])
        if migration is None:
            return state, dict(idle, state='WAITING_HISTORY_MIGRATION'), None
        state = import_legacy(state, *migration)
    ingest(state, reports, now)
    formal_raw = (root / 'tv-core.m3u').read_bytes()
    current = unique_core_routes(formal_raw)
    formal_sha = sha256_bytes(formal_raw)
    feedback = root / 'config/home-route-feedback.json'
    feedback_sha = sha256_bytes(feedback.read_bytes())
    manifest_raw = (root / 'harvest/home-candidates.json').read_bytes()
    manifest = validate_candidate_manifest(json.loads(manifest_raw))
    intake_current = manifest['formal_playlist']['sha256'] == formal_sha
    if intake_current:
        for row in manifest['candidates']:
            if row['candidate_id'] not in state['tested']:
                state['queue'][row['candidate_id']] = row
    # Receipt explicitly describes CLOUD persistence, never household qualification.
    state['delivery_receipt'] = {'schema': 'iptv-cloud-delivery-receipt/v1', 'probe_id': state['probe_id'],
        'meaning': 'cloud_queue_persisted_not_tested',
        'received_batches': [row['id'] for row in manifest.get('delivery_batches', [])] if intake_current else []}
    if intake_current and not state['delivery_receipt']['received_batches'] and manifest['candidates']:
        state['delivery_receipt']['received_batches'] = [digest(manifest['candidates'])]
    if active is None:
        return state, idle, None
    kind, day, deadline = active
    slot_id = day + '-' + kind
    if slot_id in state['completed_slots']:
        return state, dict(idle, state='SLOT_COMPLETE'), None
    cycle_id = digest([slot_id, formal_sha, feedback_sha])
    if not state.get('cycle') or state['cycle']['id'] != cycle_id:
        state['cycle'] = {'id': cycle_id, 'slot': slot_id, 'kind': kind, 'started': now,
            'binding': {'url': 'https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u', 'sha256': formal_sha, 'channel_count': len(current)},
            'feedback_sha': feedback_sha, 'manifest_sha': sha256_bytes(manifest_raw),
            'current': {key: {'url': value.url} for key,value in current.items()},
            'results': {}, 'task_rows': {}, 'batch_ids': [],
            'quality_policy': state.get('quality_policy', {}),
            'viewer_good_urls': [r.get('url') if isinstance(r, dict) else r
                for entries in json.loads(feedback.read_bytes()).get('good', {}).values() for r in entries]}
    cycle = state['cycle']
    if cycle.get('outstanding'):
        task = state['batches'][cycle['outstanding']]
        if now < epoch(task['expires_utc']):
            return state, task, None
        cycle.pop('outstanding')
    if now < cycle.get('retry_after', 0):
        return state, dict(idle, state='WAITING_HOME_RESOURCES', last_heartbeat=state.get('last_heartbeat')), None
    tasks = next_tasks(state, cycle, config, current, feedback, now)
    if not tasks:
        report = complete_report(state, cycle, current, feedback, now)
        state['completed_slots'] = (state['completed_slots'] + [slot_id])[-42:]
        state['last_report'] = report
        return state, dict(idle, state='SLOT_COMPLETE'), report
    if deadline - now < 60:
        return state, dict(idle, state='WINDOW_ENDING'), None
    task = seal_task({'schema': TASK_SCHEMA, 'probe_id': state['probe_id'], 'cycle_id': cycle_id,
        'route_context': ROUTE_CONTEXT, 'run_kind': kind, 'created_utc': timestamp(now),
        'expires_utc': timestamp(min(deadline, now + 30 * 60)), 'formal_playlist': cycle['binding'],
        'feedback_sha256': feedback_sha, 'tasks': tasks,
        'limits': {'seconds': min(240, config['batch_seconds']), 'bytes': min(64 * 1024 * 1024, config['batch_bytes'])}})
    validate_task(task, state['probe_id'], now)
    if config.get('router_runtime') == 'native-v1':
        # Admission lives in GitHub's durable state, never in router Python.
        budget = state.setdefault('native_budget', {})
        if budget.get('day') != day:
            budget = state['native_budget'] = dict(day=day, bytes=0, seconds=0,
                candidates=0, discovery_bytes=0, discovery_seconds=0, reservations={})
        amount, seconds = task['limits']['bytes'], task['limits']['seconds']
        candidates = sum(t['role'] == 'candidate' for t in tasks)
        if (budget['bytes'] + amount > config['daily_bytes'] or
                budget['seconds'] + seconds > config['daily_seconds'] or
                budget['candidates'] + candidates > config['daily_candidates'] or
                (candidates and (budget['discovery_bytes'] + amount > config['discovery_bytes'] or
                    budget['discovery_seconds'] + seconds > config['discovery_seconds']))):
            if all(t['role'] != 'current' for t in tasks):
                cycle['optional_closed'] = True
                report = complete_report(state, cycle, current, feedback, now)
                state['completed_slots'] = (state['completed_slots'] + [slot_id])[-42:]
                state['last_report'] = report
                return state, dict(idle, state='SLOT_COMPLETE'), report
            return state, dict(idle, state='WAITING_BUDGET'), None
        budget['bytes'] += amount
        budget['seconds'] += seconds
        budget['candidates'] += candidates
        if candidates:
            budget['discovery_bytes'] += amount
            budget['discovery_seconds'] += seconds
        budget['reservations'][task['batch_id']] = dict(bytes=amount, seconds=seconds, candidates=candidates)
    state['batches'][task['batch_id']] = task
    cycle['outstanding'] = task['batch_id']
    cycle['batch_ids'].append(task['batch_id'])
    return state, task, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo-root', type=Path, default=ROOT)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--control', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((args.repo_root / 'config/home-thin.json').read_bytes())
    state_path = args.control / 'state.json'
    state = json.loads(state_path.read_bytes()) if state_path.exists() else new_state(config['probe_id'])
    native_done = []
    if config.get('router_runtime') == 'native-v1' and os.environ.get('IPTV_NATIVE_INBOX'):
        from scripts.home_native_evidence import prepare_assets
        native_done = prepare_assets(state, os.environ['IPTV_NATIVE_INBOX'], args.reports, time.time())
    state, task, report = step(state, config, args.repo_root, args.reports, time.time())
    atomic_json(state_path, state)
    atomic_json(args.control / 'tasks' / (config['probe_id'] + '.json'), task)
    if config.get('router_runtime') == 'native-v1':
        from scripts.home_native_evidence import task_wire
        (args.control / 'tasks' / (config['probe_id'] + '.native')).write_bytes(
            task_wire(task, int(os.environ.get('IPTV_NATIVE_RELEASE_ID', '0'))))
        if os.environ.get('IPTV_NATIVE_INBOX'):
            (Path(os.environ['IPTV_NATIVE_INBOX'])/'processed.txt').write_text('\n'.join(native_done)+'\n')
    atomic_json(args.control / 'status.json', {'state': task.get('state', 'WAITING_MEASUREMENTS'),
        'history_migrated': state['migration_complete'], 'queue': len(state['queue']),
        'tested': len(state['tested']), 'last_heartbeat': state.get('last_heartbeat')})
    if state.get('delivery_receipt'):
        atomic_json(args.control / 'delivery-receipt.json', state['delivery_receipt'])
    # Regenerate the last deterministic report if a prior multi-branch push was interrupted.
    report = report or state.get('last_report')
    if report:
        raw = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        destination = args.reports / 'inbox' / config['probe_id'] / report_filename(report, raw)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != raw:
            raise RuntimeError('derived report changed')
        destination.write_bytes(raw)
    print('HOME_CLOUD', task.get('state', 'WAITING_MEASUREMENTS'), 'history_migrated=' + str(state['migration_complete']))


if __name__ == '__main__':
    main()
