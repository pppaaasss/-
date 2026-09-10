"""Text-only delivery polling. Receiving a batch never grants playback health."""
import hashlib
import json
from pathlib import Path


def batch_ids(manifest):
    try:
        from .home_contract import object_sha256
    except ImportError:
        from home_contract import object_sha256
    batches = manifest.get('delivery_batches')
    if batches is None:
        return [object_sha256(manifest['candidates'])] if manifest['candidates'] else []
    known = {row['candidate_id'] for row in manifest['candidates']}
    ids = []
    for batch in batches:
        ident = batch['id']
        if (not isinstance(ident, str) or len(ident) != 64
                or any(c not in '0123456789abcdef' for c in ident)
                or not set(batch['candidate_ids']).issubset(known)):
            raise ValueError('invalid delivery batch')
        ids.append(ident)
    return ids


def save_receipts(root, state, manifest, config, epoch):
    try:
        from .home_probe import atomic_json
    except ImportError:
        from home_probe import atomic_json
    ids = batch_ids(manifest)
    receipt = dict(schema='iptv-home-delivery-receipt/v1', probe_id=config.get('probe_id'),
                   received_batches=ids, received_epoch=epoch,
                   queue_remaining=len(state.get('candidate_queue') or []),
                   meaning='queue_persisted_not_tested')
    path = root / 'candidate-receipts.json'
    if path.exists():
        old = json.loads(path.read_text())
        if old.get('received_batches') == ids and old.get('probe_id') == config.get('probe_id'):
            return old
    atomic_json(path, receipt)
    return receipt


def receive(config, root, epoch):
    try:
        from . import home_probe as probe
        from .candidate_history import unseen_candidates
        from .progress_journal import replay_progress
    except ImportError:
        import home_probe as probe
        from candidate_history import unseen_candidates
        from progress_journal import replay_progress
    url = str(config.get('candidate_manifest_url', probe.DEFAULT_CANDIDATE_MANIFEST)).strip()
    if not url:
        return False
    manifest, raw, _ = probe.fetch_candidate_manifest(url, now_epoch=epoch,
        max_age_hours=float(config.get('candidate_manifest_max_age_hours') or 48))
    batch_ids(manifest)
    formal, _, _ = probe.fetch_playlist(str(config.get('playlist_url') or probe.DEFAULT_PLAYLIST))
    entries = probe.parse_playlist(formal)
    if (manifest['formal_playlist']['sha256'] != hashlib.sha256(formal).hexdigest()
            or manifest['formal_playlist']['channel_count'] != len(entries)):
        raise RuntimeError('candidate_manifest_formal_playlist_changed')
    state = replay_progress(probe.load_json(root / 'state.json'), root)
    digest = hashlib.sha256(raw).hexdigest()
    receipt_path = root / 'candidate-receipts.json'
    old_receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    if (digest == state.get('last_candidate_manifest_sha256')
            and old_receipt.get('received_batches') == batch_ids(manifest)
            and old_receipt.get('probe_id') == config.get('probe_id')):
        return bool(state.get('candidate_queue'))
    incoming = ([dict(row, source_manifest_sha256=digest, _queue_priority=1) for row in manifest['candidates']]
                if digest != state.get('last_candidate_manifest_sha256') else [])
    current = {probe.station_key(name): url for name, url in entries}
    queue = probe.merge_candidate_queue(state.get('candidate_queue'), incoming, current)
    tested = set(state.get('tested_candidate_ids') or []) | set(state.get('candidate_observations') or {})
    queue = unseen_candidates(queue, tested)
    state.update(candidate_queue=queue, last_candidate_manifest_sha256=digest,
                 last_candidate_manifest_utc=manifest['generated_utc'])
    probe.atomic_json(root / 'state.json', state)
    save_receipts(root, state, manifest, config, epoch)
    return bool(queue)


if __name__ == '__main__':
    import sys
    request = json.load(sys.stdin)
    pending = receive(request['config'], Path(request['root']), request['epoch'])
    print(json.dumps(dict(pending=pending)))
