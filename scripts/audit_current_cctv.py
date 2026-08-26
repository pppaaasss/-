#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from probe_cctv5_hd import probe

PLAYLIST=Path('tv.m3u')
REPORT=Path('cctv-current-quality-report.txt')


def load_cctv():
    lines=PLAYLIST.read_text(encoding='utf-8').splitlines()
    rows=[]
    for i,line in enumerate(lines):
        if not line.startswith('#EXTINF:') or ',' not in line:
            continue
        name=line.rsplit(',',1)[1].strip()
        if not (re.fullmatch(r'CCTV-(?:[1-9]|1[0-7])',name) or name in {'CCTV-5+','CCTV-4K'}):
            continue
        j=i+1
        while j<len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
            j+=1
        if j<len(lines):
            rows.append((name,lines[j].strip()))
    return rows


def one(item):
    name,url=item
    r=probe(name,url)
    d=asdict(r)
    d['name']=name
    min_dl=min(r.download_mbps,r.second_download_mbps or r.download_mbps) if r.ok else 0.0
    d['min_download_mbps']=round(min_dl,3)
    d['headroom']=round(min_dl/r.stream_mbps,3) if r.ok and r.stream_mbps else 0.0
    low=url.lower()
    m=re.search(r'/([0-9]{2,5})/index\.m3u8',low)
    d['url_profile']=int(m.group(1)) if m else None
    return d


def key(row):
    n=row['name']
    if n=='CCTV-5+': return 5.5
    if n=='CCTV-4K': return 99
    return int(n.split('-')[1])


def main():
    items=load_cctv()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        rows=list(ex.map(one,items))
    rows.sort(key=key)
    payload={
        'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        'channels':len(rows),
        'results':rows,
    }
    REPORT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for x in rows:
        if x['ok']:
            print(f"{x['name']:8} {x['width']}x{x['height']} {x['mode']:10} stream={x['stream_mbps']:.2f}M dl={x['min_download_mbps']:.2f}M profile={x['url_profile']} host={x['url'].split('/')[2]}")
        else:
            print(f"{x['name']:8} FAIL profile={x['url_profile']} {x['error'][:120]}")
    return 0

if __name__=='__main__':
    raise SystemExit(main())
