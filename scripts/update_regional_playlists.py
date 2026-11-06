#!/usr/bin/env python3
"""Render reviewed regional selections through the existing playlist writer.

Offline only: no probing, household qualification, publication, or core edits.
Unrelated EXTINF blocks retain their exact bytes and relative order.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_playlist as bp
from router.ac86u.regional_channels import CHANNELS, regional_key, route_equivalence_key


def blocks(text):
    parts = re.split(r'(?m)(?=^#EXTINF:)', text)
    return parts[0], parts[1:]


def block_key(block):
    return regional_key(block.splitlines()[0].rsplit(',', 1)[-1])


def render(routes, path):
    channels = []
    options = {}
    for key, row in routes.items():
        channel = CHANNELS[key]
        opts = row.get('request_options', '')
        # Only the exact reviewed public VLC Referer option is supported here.
        # Native household admission continues to reject request_options.
        if opts and not re.fullmatch(r'#EXTVLCOPT:http-referrer=https://[a-z0-9.-]+/[^\r\n]*', opts):
            raise ValueError('unsupported regional playback options')
        channels.append(bp.Channel(name=channel['name'], extinf='#EXTINF:-1,'+channel['name'],
                                   url=row['url'], group=channel['group']))
        options[key] = opts
    bp.write_playlist(path, channels, '台湾、日本：本机媒体复核；家庭线路待验')
    header, rendered = blocks(path.read_text())
    result = {}
    for block in rendered:
        key = block_key(block)
        if options[key]:
            first, rest = block.split('\n', 1)
            block = first+'\n'+options[key]+'\n'+rest
        result[key] = block
    return header, result


def merge_playlist(text, rendered, epg_urls):
    header, old = blocks(text)
    output, seen = [], set()
    for block in old:
        key = block_key(block)
        if key in rendered:
            if key not in seen:
                output.append(rendered[key])
                seen.add(key)
        else:
            output.append(block)
    for key in CHANNELS:
        if key in rendered and key not in seen:
            output.append(rendered[key])
    match = re.search(r'x-tvg-url="([^"]*)"', header)
    urls = list(dict.fromkeys([*(match.group(1).split(',') if match else [bp.EPG_URL]), *epg_urls]))
    if match:
        header = header[:match.start(1)]+','.join(urls)+header[match.end(1):]
    else:
        header = header.replace('#EXTM3U', '#EXTM3U x-tvg-url="'+','.join(urls)+'"', 1)
    header = re.sub(r'channels=\d+', 'channels='+str(len(output)), header)
    return header+''.join(output)


def update(root):
    data = json.loads((root/'config/regional-streams.json').read_text())
    primary, backups = {}, {}
    for row in data['channels']:
        key = row['channel_key']
        if key not in CHANNELS:
            raise ValueError('unknown regional channel')
        accepted = [row['primary']] if row.get('primary') else []
        accepted += row.get('backups', [])
        identities = set()
        for route in accepted:
            if (route.get('verification') != 'CODEX_VERIFIED' or route.get('household_status') != 'UNVERIFIED'
                    or len(set(route.get('successful_rounds', []))) < 2 or not route.get('identity_review')):
                raise ValueError('missing regional media/identity review')
            identity = route_equivalence_key(key, route['url'])
            if identity in identities:
                raise ValueError('primary/backup refer to the same regional stream')
            identities.add(identity)
        if row.get('primary'):
            primary[key] = row['primary']
        # A second playlist is a BERO preview, never a home-qualified archive.
        for index, route in enumerate(row.get('backups', [])):
            backups[(key,index)] = route
    with tempfile.TemporaryDirectory(prefix='regional-render-') as directory:
        header, rendered = render(primary, Path(directory)/'regional.m3u')
        header = re.sub(r'(?m)^# generated_utc=.*$', '# generated_utc='+data['generated_utc'], header)
        epg_urls = list(dict.fromkeys(CHANNELS[key]['epg_url'] for key in primary if CHANNELS[key].get('epg_url')))
        for name in ('tv.m3u', 'tv-all.m3u', 'tv-easy.m3u'):
            path = root/name
            path.write_text(merge_playlist(path.read_text(), rendered, epg_urls), encoding='utf-8')
        preview = root/'candidate/tw-jp.m3u'
        preview.parent.mkdir(exist_ok=True)
        preview.write_text(header+''.join(rendered.values()), encoding='utf-8')
        alternate_blocks = []
        for (key, _), route in backups.items():
            _, alternate = render({key: route}, Path(directory)/'alternate.m3u')
            alternate_blocks.append(alternate[key])
        backup_header = re.sub(r'(?m)^# channels=\d+$', '# channels='+str(len(alternate_blocks)), header)
        (root/'candidate/tw-jp-backup.m3u').write_text(backup_header+''.join(alternate_blocks), encoding='utf-8')
    return dict(reviewed_primary_count=len(primary), local_backup_count=len(backups),
                household_qualified=False, core_modified=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(update(args.root), ensure_ascii=False))


if __name__ == '__main__':
    main()
