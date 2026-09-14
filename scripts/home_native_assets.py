"""GitHub Actions-only temporary sample inbox; never commits media bytes."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

REPO = 'repos/pppaaasss/-'
TAG = 'iptv-native-sample-inbox-v1'
MAXIMUM = 768 * 1024


def api(path, method='GET', payload=None):
    command = ['gh', 'api', REPO+'/'+path, '--method', method]
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload).encode() if payload else None,
        capture_output=True, check=True, timeout=45)
    return json.loads(result.stdout) if result.stdout.strip() else None


def prepare(directory):
    releases = api('releases?per_page=100')
    matching = [r for r in releases if r['tag_name'] == TAG and r['draft']]
    if len(matching) > 1:
        raise ValueError('ambiguous native inbox release')
    release = matching[0] if matching else api('releases', 'POST', {
        'tag_name': TAG, 'target_commitish': 'master', 'name': 'IPTV temporary household samples',
        'draft': True, 'body': 'Temporary native measurement inbox. Assets are removed after durable cloud ingestion. Not a software release.'})
    assets = api('releases/'+str(release['id'])+'/assets?per_page=100')
    index = {}
    for asset in assets:
        name = asset['name']
        if not re.fullmatch(r'[0-9a-f]{64}-[0-9a-f]{64}\.native', name):
            continue
        if not 0 < asset['size'] <= MAXIMUM:
            raise ValueError('native asset too large')
        target = directory/name
        with target.open('wb') as output:
            subprocess.run(['gh', 'api', REPO+'/releases/assets/'+str(asset['id']),
                '-H', 'Accept: application/octet-stream'], stdout=output, check=True, timeout=45)
        if target.stat().st_size > MAXIMUM:
            raise ValueError('native download exceeds bound')
        index[name] = asset['id']
    (directory/'asset-index.json').write_text(json.dumps(index))
    with open(os.environ['GITHUB_ENV'], 'a') as env:
        env.write(f'IPTV_NATIVE_RELEASE_ID={release["id"]}\nIPTV_NATIVE_INBOX={directory}\n')
    print('Native inbox ready; temporary assets:', len(index))


def cleanup(directory):
    # Called only after report AND controller commits have been pushed.
    index = json.loads((directory/'asset-index.json').read_text())
    processed = directory/'processed.txt'
    if processed.exists():
        for name in processed.read_text().splitlines():
            if name in index:
                api('releases/assets/'+str(index[name]), 'DELETE')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare','cleanup'))
    parser.add_argument('--directory', type=Path, default=Path('/tmp/iptv-native-inbox'))
    args = parser.parse_args()
    config = json.loads(Path('config/home-thin.json').read_bytes())
    if config.get('enabled') and config.get('router_runtime') == 'native-v1':
        args.directory.mkdir(parents=True, exist_ok=True)
        (prepare if args.action == 'prepare' else cleanup)(args.directory)
