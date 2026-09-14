#!/usr/bin/env python3
"""Phone-side, resumable history upload; never send router configuration or keys."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from router.ac86u.thin_api import GithubData, read_token
from router.ac86u.thin_contract import encode, PROBE

FILES = ('state.json', 'qualified-backups.json', 'pipeline-trial/state.json',
         'pipeline-trial/qualified-backups.json', 'progress.jsonl', 'quality-policy.json')
MAX_FILE = 32 * 1024 * 1024


def prepare(directory):
    if not (directory / 'state.json').is_file():
        raise ValueError('household state.json is required; never substitute an empty object')
    manifest, chunks = {'schema': 'iptv-home-migration/v1', 'files': []}, {}
    for name in FILES:
        path = directory / name
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_size > MAX_FILE:
            raise ValueError('unsafe or oversized migration file: ' + name)
        raw = path.read_bytes()
        if name == 'progress.jsonl':
            for line in raw.splitlines(keepends=True):
                if line.endswith(b'\n'):
                    json.loads(line)
        elif not isinstance(json.loads(raw), dict):
            raise ValueError('history object required: ' + name)
        packed = gzip.compress(raw, compresslevel=6, mtime=0)
        parts = []
        for offset in range(0, len(packed), 32768):
            chunk = packed[offset:offset+32768]
            identity = hashlib.sha256(chunk).hexdigest()
            chunks[identity] = encode({'data': base64.b64encode(chunk).decode('ascii')})
            parts.append(identity)
        if len(parts) > 1024:
            raise ValueError('compressed history exceeds migration bound')
        manifest['files'].append({'name': name, 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'parts': parts})
    return manifest, chunks


def upload(directory, token_path, probe):
    if not PROBE.fullmatch(probe):
        raise ValueError('invalid probe identity')
    manifest, chunks = prepare(directory)
    api = GithubData(read_token(token_path))
    destination = 'migrations/' + probe + '/'
    # All content is immutable. Retrying after a lost reply proves byte identity.
    for identity, raw in sorted(chunks.items()):
        path = destination + identity + '.json'
        existing = api.get('home-reports', path)
        if existing is not None:
            if existing != raw:
                raise ValueError('immutable migration chunk changed')
            continue
        api.put_immutable(path, raw)
        time.sleep(1)  # Pace one-time migration writes; retries reuse existing chunks.
    api.put_immutable(destination + 'manifest.json', encode(manifest))
    api.notify()
    print('HISTORY_UPLOADED; wait for cloud status history_migrated=true')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--probe-id', default='home-ac86u-8f8908f0fba9')
    args = parser.parse_args()
    upload(args.directory, args.token_file, args.probe_id)
