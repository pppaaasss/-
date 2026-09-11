"""Persistent discovery history; old evidence is never given a new timestamp."""
import json
import re
from pathlib import Path

try:
    from .home_contract import validate_backup_pool
except ImportError:
    from home_contract import validate_backup_pool


def prepare_history(state, root, probe_id, now_epoch, minimum_headroom=1.05):
    state = dict(state)
    tested = set(state.get('tested_candidate_ids') or [])
    tested.update((state.get('candidate_observations') or {}).keys())
    archive = dict(state.get('backup_archive') or {})
    trial = Path(root) / 'pipeline-trial'
    # Journal records may contain newer evidence than the last state snapshot.
    # Keep only checkpoints measured under this policy, including such records.
    if 'current_checkpoints' in state:
        state['current_checkpoints'] = {key: value for key, value in state['current_checkpoints'].items()
            if value.get('headroom_policy', 1.35) == minimum_headroom}
    state['headroom_policy'] = minimum_headroom
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
    if not state.get('cctv4k_1080_history_imported'):
        # The owner now accepts 1080p CCTV-4K. Retain measurements previously
        # rejected solely by the 2160-line floor as on-demand repair candidates,
        # without replaying discovery or manufacturing a fresh qualification.
        observations = dict(state.get('candidate_observations') or {})
        source = trial / 'state.json'
        if source.exists():
            old_observations = json.loads(source.read_text()).get('candidate_observations') or {}
            observations = dict(old_observations, **observations)
        for identity, observation in observations.items():
            candidate = observation.get('candidate') or {}
            result = observation.get('result') or {}
            verification = result.get('verification') or {}
            if (candidate.get('channel_key') == 'cctv4k'
                    and observation.get('qualification') == 'REJECTED'
                    and str(result.get('error') or '').startswith('decoded_height_')
                    and str(result.get('error') or '').endswith('_below_2160')
                    and int(verification.get('height') or 0) >= 1080):
                archive.setdefault(identity, dict(candidate, verification=dict(verification),
                    archived_reason='previous_2160_floor',
                    last_checked_utc=observation.get('last_checked_utc')))
        state['cctv4k_1080_history_imported'] = True
    if state.get('headroom_history_imported') != minimum_headroom:
        observations = {}
        source = trial / 'state.json'
        if source.exists():
            observations.update(json.loads(source.read_text()).get('candidate_observations') or {})
        # A later formal observation supersedes a trial observation.
        observations.update(state.get('candidate_observations') or {})
        recovered = []
        for identity, observation in observations.items():
            candidate = observation.get('candidate') or {}
            result = observation.get('result') or {}
            verification = result.get('verification') or {}
            codec = str(verification.get('codec') or '').casefold()
            bitrate = verification.get('stream_mbps') or verification.get('bitrate_mbps') or 0
            if (candidate.get('candidate_id') == identity and candidate.get('url')
                    and observation.get('qualification') == 'REJECTED'
                    and re.fullmatch(r'headroom_[0-9.]+_below_[0-9.]+', str(result.get('error') or ''))
                    and verification.get('deep_checked') is True
                    and int(verification.get('sample_count') or 0) == 2
                    and int(verification.get('height') or 0) >= 1080
                    and float(bitrate) >= (2.5 if codec in {'h265', 'hevc'} else 3.0)
                    and float(verification.get('headroom_ratio') or 0) >= minimum_headroom
                    and identity not in archive):
                archive[identity] = dict(candidate, verification=dict(verification),
                    archived_reason='previous_headroom_floor',
                    last_checked_utc=observation.get('last_checked_utc'))
                recovered.append(identity)
        # Archive only: no fresh time, qualification or discovery rescan.
        # Normal repairs still apply feedback/identity vetoes and recheck media.
        state['headroom_history_imported'] = minimum_headroom
        state['headroom_history_recovered_ids'] = recovered
    state['tested_candidate_ids'] = sorted(tested)
    state['backup_archive'] = archive
    state['candidate_queue'] = unseen_candidates(state.get('candidate_queue') or [], tested)
    return state


def unseen_candidates(rows, tested):
    return [row for row in rows if row['candidate_id'] not in tested]
