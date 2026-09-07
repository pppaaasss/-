"""Persistent discovery history; old evidence is never given a new timestamp."""
import json
from pathlib import Path

try:
    from .home_contract import validate_backup_pool
except ImportError:
    from home_contract import validate_backup_pool


def prepare_history(state, root, probe_id, now_epoch):
    state = dict(state)
    tested = set(state.get('tested_candidate_ids') or [])
    tested.update((state.get('candidate_observations') or {}).keys())
    archive = dict(state.get('backup_archive') or {})
    trial = Path(root) / 'pipeline-trial'
    if not state.get('trial_history_imported') and trial.exists():
        source = trial / 'state.json'
        if source.exists():
            old = json.loads(source.read_text())
            # Only actual observations count. A previous manifest is not evidence
            # that every listed address was attempted.
            tested.update((old.get('candidate_observations') or {}).keys())
        source = trial / 'qualified-backups.json'
        if source.exists():
            pool = json.loads(source.read_text())
            validate_backup_pool(pool, expected_probe_id=probe_id,
                                 now_epoch=now_epoch, allow_expired=True, trial=True)
            for row in pool['backups']:
                tested.add(row['candidate_id'])
                archive.setdefault(row['candidate_id'], dict(row))
        state['trial_history_imported'] = True
    state['tested_candidate_ids'] = sorted(tested)
    state['backup_archive'] = archive
    state['candidate_queue'] = unseen_candidates(state.get('candidate_queue') or [], tested)
    return state


def unseen_candidates(rows, tested):
    return [row for row in rows if row['candidate_id'] not in tested]
