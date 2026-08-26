#!/usr/bin/env python3
"""Surgical repair for viewer-verified TV identities.

Only the URL following an exact EXTINF display name is replaced. No grouping,
metadata, ordering, channel count, or unrelated station is changed.
"""
from __future__ import annotations
from pathlib import Path

PLAYLISTS=(Path('tv-easy.m3u'),Path('tv.m3u'),Path('tv-all.m3u'),Path('tv-core.m3u'))

# All entries here must have passed ffprobe + two HLS segment downloads on
# GitHub Actions. China Mobile PLTV/GMCC routes are intentionally excluded.
VERIFIED={
    '湖南卫视':'http://192.151.150.154/live/hnwshd.m3u8',
    '黑龙江卫视':'http://107.150.60.122/live/hljwshd.m3u8',
    '海南卫视':'http://107.150.60.122/live/lywshd.m3u8',
}

# Known identity traps are documented here so future changes do not re-add them.
FORBIDDEN_BY_NAME={
    'CCTV-8':('cctv8k',),
    '黑龙江卫视':('SXyuyao3',),
}

def display(line:str)->str|None:
    if not line.startswith('#EXTINF:') or ',' not in line: return None
    return line.rsplit(',',1)[1].strip()

def patch(path:Path)->int:
    if not path.exists(): return 0
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    changed=0
    for i,line in enumerate(lines):
        name=display(line.rstrip('\r\n'))
        replacement=VERIFIED.get(name or '')
        if not replacement: continue
        j=i+1
        while j<len(lines) and lines[j].lstrip().startswith('#'): j+=1
        if j>=len(lines): continue
        old=lines[j].rstrip('\r\n')
        ending='\r\n' if lines[j].endswith('\r\n') else '\n'
        if old!=replacement:
            lines[j]=replacement+ending
            changed+=1
    if changed: path.write_text(''.join(lines),encoding='utf-8',newline='')
    return changed

def main()->int:
    total=0
    for p in PLAYLISTS:
        n=patch(p); total+=n; print(f'{p}: changed={n}')
    print(f'total_changed={total}')
    return 0
if __name__=='__main__': raise SystemExit(main())
