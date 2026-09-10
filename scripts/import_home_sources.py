#!/usr/bin/env python3
"""Queue user-supplied playlists for existing home qualification, without granting health."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_home_candidate_manifest import build_manifest, playlist_entries, retain_delivery, atomic_json
from scripts.home_route_policy import rejected_urls, rejected_hosts
from router.ac86u.home_contract import object_sha256, utc_text, validate_candidate_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+')
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    target = ROOT / 'harvest/home-candidates.json'
    index_path = ROOT / 'harvest/home-candidate-index.json'
    old = json.loads(target.read_text())
    index = json.loads(index_path.read_text())
    validate_candidate_manifest(old)
    rows, files = [], []
    for filename in args.files:
        path = Path(filename)
        raw = path.read_bytes()
        parsed = playlist_entries(raw)
        digest = hashlib.sha256(raw).hexdigest()
        files.append(dict(name=path.name, sha256=digest, entries=len(parsed)))
        for name, url in parsed:
            rows.append(dict(name=name, url=url, sources=['user-upload:' + path.name + ':sha256:' + digest]))
    feedback = ROOT / 'config/home-route-feedback.json'
    kwargs = dict(formal_bytes=(ROOT / 'tv-core.m3u').read_bytes(),
                  formal_url=old['formal_playlist']['url'], source_revision=args.source_revision,
                  generated_utc=utc_text(), rejected_urls=rejected_urls(feedback),
                  rejected_hosts=rejected_hosts(feedback))
    incoming, summary = build_manifest(discovery_rows=rows, **kwargs)
    # Check labels against both this import and already pending candidates.
    combined, _ = build_manifest(discovery_rows=old['candidates'] + incoming['candidates'], **kwargs)
    allowed = {r['candidate_id'] for r in combined['candidates']}
    old_ids = {r['candidate_id'] for r in old['candidates']}
    additions = [r for r in incoming['candidates'] if r['candidate_id'] in allowed and r['candidate_id'] not in old_ids]
    delta = dict(incoming, candidates=additions, candidate_count=len(additions), candidate_set_sha256=object_sha256(additions))
    # Preserve every unacknowledged batch and the daily discovery snapshot index.
    output, new_index = retain_delivery(delta, index, index, old)
    report = dict(schema='iptv-user-source-import/v1', generated_utc=kwargs['generated_utc'],
                  files=files, input_entries=len(rows), eligible_unique=len(incoming['candidates']),
                  already_pending=sum(r['candidate_id'] in old_ids for r in incoming['candidates']),
                  cross_batch_conflicts=sum(r['candidate_id'] not in allowed for r in incoming['candidates']),
                  newly_queued=len(additions), total_delivery_candidates=output['candidate_count'],
                  filters=summary['rejected'], playback_verified=False,
                  source_rows=rows)
    validate_candidate_manifest(output)
    assert old_ids <= {r['candidate_id'] for r in output['candidates']}
    atomic_json(target, output)
    atomic_json(index_path, new_index)
    atomic_json(Path(args.report), report)
    print(json.dumps({k:v for k,v in report.items() if k != 'source_rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
