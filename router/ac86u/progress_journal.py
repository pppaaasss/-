"""Fsync one completed address; replay is idempotent and preserves evidence time."""
import json
import os


def append_progress(root, record):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'progress.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def replay_progress(state, root):
    path = root / 'progress.jsonl'
    if not path.exists():
        return state
    # A torn final record has no completed evidence and may be retried. Any
    # corruption in an earlier complete record stops recovery for diagnosis.
    with path.open('r+b') as stream:
        complete_offset = 0
        for raw in stream:
            if not raw.endswith(b'\n'):
                stream.truncate(complete_offset)
                stream.flush()
                os.fsync(stream.fileno())
                break
            complete_offset += len(raw)
            row = json.loads(raw)
            kind = row['kind']
            if kind == 'candidate':
                identity = row['identity']
                state.setdefault('candidate_observations', {})[identity] = row['observation']
                tested = set(state.get('tested_candidate_ids') or [])
                tested.add(identity)
                state['tested_candidate_ids'] = sorted(tested)
                state['candidate_queue'] = [c for c in state.get('candidate_queue', []) if c['candidate_id'] != identity]
                if row.get('backup'):
                    state.setdefault('backup_archive', {})[identity] = row['backup']
            elif kind == 'backup_success':
                if row.get('backup'):
                    state.setdefault('backup_archive', {})[row['identity']] = row['backup']
                if row.get('cycle'):
                    state.setdefault('current_checkpoints', {}).setdefault(row['cycle'], {}).setdefault('switches', {})[row['identity']] = row['switch']
            elif kind == 'backup_failure':
                state.setdefault('backup_archive', {})[row['identity']] = row['backup']
            elif kind == 'current':
                checkpoint = state.setdefault('current_checkpoints', {}).setdefault(row['cycle'], {})
                if (checkpoint.get('formal_sha') != row['formal_sha']
                        or checkpoint.get('headroom_policy', 1.35) != row.get('headroom_policy', 1.35)):
                    checkpoint.clear()
                checkpoint.update(formal_sha=row['formal_sha'], updated=row['epoch'],
                                  feedback_signature=row.get('feedback_signature', {}),
                                  headroom_policy=row.get('headroom_policy', 1.35))
                checkpoint.setdefault('attempts', {})[row['key']] = row['attempts']
                checkpoint.setdefault('times', {})[row['key']] = row['epoch']
    return state


def clear_progress(root):
    path = root / 'progress.jsonl'
    with path.open('w') as stream:
        stream.flush()
        os.fsync(stream.fileno())
