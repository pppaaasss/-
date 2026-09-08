#!/usr/bin/env python3
"""Exact URL coverage inventory; never copies health across different routes."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.build_home_candidate_manifest import playlist_entries
from router.ac86u.home_contract import station_key


def inventory(raw):
    found={}
    for name,url in playlist_entries(raw):
        key=station_key(name)
        if key: found.setdefault(key,[]).append(dict(name=name,url=url))
    return found


def audit(root, report=None):
    core=inventory((root/'tv-core.m3u').read_bytes())
    evidence={(r['channel_key'],r['url']):r for r in (report or {}).get('current_results',[])}
    result={}
    for filename in ('tv-easy.m3u','tv.m3u','tv-all.m3u'):
        rows=inventory((root/filename).read_bytes()); differences=[]
        for key,items in rows.items():
            for item in items:
                if item['url'] not in {r['url'] for r in core.get(key,[])}:
                    exact=evidence.get((key,item['url']))
                    differences.append(dict(channel=key,url=item['url'],core_urls=[r['url'] for r in core.get(key,[])],
                        coverage='independent_url_not_in_core',
                        evidence_status=exact['status'] if exact else 'UNVERIFIED',
                        evidence_time=(report or {}).get('generated_utc') if exact else None,
                        disposition='preserve_pending_subscription_confirmation'))
        result[filename]=dict(differences=differences,missing=sorted(set(core)-set(rows)),
            duplicates={k:v for k,v in rows.items() if len(v)>1})
    return dict(schema='iptv-home-coverage/v1',scope='static URL coverage; no playback test',playlists=result)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(ROOT));parser.add_argument('--report')
    args=parser.parse_args();report=json.loads(Path(args.report).read_text()) if args.report else None
    print(json.dumps(audit(Path(args.root),report),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
