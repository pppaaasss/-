#!/usr/bin/env python3
"""Select emergency CCTV alternatives from the already-completed portion of the HK full audit.

Read-only: this script does not probe streams and does not modify playlists. It is safe to run
while hk_audit_all_urls.py is still appending results.jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import hk_probe
import hk_filter_harvest as hf

AUDIT = Path('/var/lib/iptv-hk-probe/all-url-audit/results.jsonl')
FORMAL = Path('tv-core.m3u')
FEEDBACK = Path('config/home-route-feedback.json')


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or '').casefold()
    except Exception:
        return ''


def score(row: dict) -> float:
    h = int(row.get('height') or 0)
    br = float(row.get('bitrate_mbps') or 0)
    dl = float(row.get('segment_mbps') or 0)
    st = float(row.get('startup_s') or 0)
    codec = str(row.get('codec') or '').casefold()
    s = h * 10 + min(br, 30.0) * 260 + min(dl, 100.0) * 8 - min(st, 30.0) * 35
    if codec == 'h264' and br == 0:
        s -= 900
    return round(s, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=1, help='diverse candidates printed per CCTV channel')
    args = ap.parse_args()
    top = max(1, min(args.top, 10))

    if not AUDIT.exists():
        raise SystemExit(f'missing {AUDIT}')
    formal = hk_probe.load_playlist(FORMAL)
    formal_urls = dict(formal)
    targets = [n for n,_ in formal if n.startswith('CCTV-')]
    matcher = hf.build_target_matcher(targets)
    bad = hf.load_bad_urls(FEEDBACK)
    buckets: dict[str,list[dict]] = defaultdict(list)
    seen = set()
    read_rows = 0
    skipped_ambiguous = 0

    with AUDIT.open('r', encoding='utf-8') as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            read_rows += 1
            if str(r.get('status') or '') != 'GOOD' or not bool(r.get('segment_ok')):
                continue
            url = str(r.get('url') or '').strip()
            if not url or url in bad or hf.TOKEN_HINT_RE.search(url):
                continue
            matched = set()
            for name in (r.get('names') or []):
                t = matcher(str(name))
                if t:
                    matched.add(t)
            if len(matched) != 1:
                if len(matched) > 1:
                    skipped_ambiguous += 1
                continue
            target = next(iter(matched))
            if url == formal_urls.get(target):
                continue
            if target == 'CCTV-8' and 'cctv8k' in url.casefold():
                continue
            height = int(r.get('height') or 0)
            floor = 2160 if target == 'CCTV-4K' else 1080
            if height < floor:
                continue
            codec = str(r.get('codec') or '').casefold()
            br = float(r.get('bitrate_mbps') or 0)
            if codec == 'h264' and 0 < br < 2.0:
                continue
            k=(target,url)
            if k in seen:
                continue
            seen.add(k)
            x=dict(r)
            x['_score']=score(r)
            buckets[target].append(x)

    print(f'PARTIAL_ROWS={read_rows} AMBIGUOUS_SKIPPED={skipped_ambiguous}')
    total=0
    for target in targets:
        rows=sorted(buckets.get(target,[]), key=lambda r:r['_score'], reverse=True)
        picked=[]; hosts=set()
        for r in rows:
            h=host_of(str(r.get('url') or ''))
            if h and h in hosts:
                continue
            picked.append(r)
            if h: hosts.add(h)
            if len(picked)>=top: break
        if not picked:
            continue
        print(f'\n[{target}] current={formal_urls.get(target,"")}')
        for i,r in enumerate(picked,1):
            print(
                f'{i}. {int(r.get("width") or 0)}x{int(r.get("height") or 0)} '
                f'{r.get("codec","")} bitrate={float(r.get("bitrate_mbps") or 0):.3f}M '
                f'dl={float(r.get("segment_mbps") or 0):.3f}M startup={float(r.get("startup_s") or 0):.3f}s '
                f'host={host_of(str(r.get("url") or ""))} score={r["_score"]} '
                f'url={r.get("url","")}'
            )
            total += 1
    print(f'\nCANDIDATES_PRINTED={total}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
