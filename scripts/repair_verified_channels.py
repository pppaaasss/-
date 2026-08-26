#!/usr/bin/env python3
"""Surgical post-build repair for verified TV routes and satellite grouping.

Exact-name rules only: pin measured routes, move known non-satellite services to
their proper groups, and drop dead/misclassified tiles from the satellite group.
Unrelated channels, ordering and metadata are left untouched.
"""
from __future__ import annotations
import re
from pathlib import Path

PLAYLISTS=(Path('tv-easy.m3u'),Path('tv.m3u'),Path('tv-all.m3u'),Path('tv-core.m3u'))

# All entries here passed ffprobe + two real HLS segment downloads on GitHub
# Actions. China Mobile PLTV/GMCC/Migu routes are intentionally excluded.
VERIFIED={
    # 1920x1080, ~6.71 Mbps stream, ~9.30 Mbps minimum measured download.
    'CCTV-5':'http://1.24.39.180:9003/hls/5/index.m3u8',
    # True 1080 satellite routes with large measured download headroom.
    '湖南卫视':'http://192.151.150.154/live/hnwshd.m3u8',
    '黑龙江卫视':'http://107.150.60.122/live/hljwshd.m3u8',
    '海南卫视':'http://107.150.60.122/live/lywshd.m3u8',
    '陕西卫视':'http://107.150.60.122/live/snwshd.m3u8',
}

# Exact services confirmed by the frame audit to be mis-grouped rather than
# mainland satellite TV stations.
REGROUP={
    '央视文化精品':'中文付费',
    '人间卫视':'台湾',
}

# Dead duplicates, non-TV scenic webcams, or broken tiles that were incorrectly
# published into 卫视台. They are removed until a real TV route is verified.
DROP_EXACT={
    'CCTV央视台球',
    '星空卫视',
    '北京衛視 (1080p) [Geo-blocked]',
    '康巴卫视',
    '央视网-云南保山瓦窑镇瓦窑码头',
    '央视网-云南普洱澜沧县景迈山',
    '央视网-云南西双版纳千年古寨曼丢',
    '央视网-安徽黄山始信新道',
    '央视网-江苏无锡鼋头渚',
    '央视网-河北邯郸京娘湖',
    '央视网-西藏日喀则珠穆朗玛峰',
    '央视网-西藏林芝岷山错高民宿',
    '央视网-西藏林芝鲁朗色季拉山',
}

# Known identity traps. Keep these documented so future candidate work does not
# accidentally re-introduce them.
FORBIDDEN_BY_NAME={
    'CCTV-8':('cctv8k',),
    '黑龙江卫视':('SXyuyao3',),
}

def display(line:str)->str|None:
    line=line.rstrip('\r\n')
    if not line.startswith('#EXTINF:') or ',' not in line: return None
    return line.rsplit(',',1)[1].strip()

def set_group(extinf:str, group:str)->str:
    ending='\r\n' if extinf.endswith('\r\n') else '\n'
    raw=extinf.rstrip('\r\n')
    if re.search(r'group-title="[^"]*"',raw):
        raw=re.sub(r'group-title="[^"]*"',f'group-title="{group}"',raw,count=1)
    else:
        comma=raw.rfind(',')
        if comma>=0:
            raw=raw[:comma]+f' group-title="{group}"'+raw[comma:]
    return raw+ending

def patch(path:Path)->tuple[int,int,int]:
    if not path.exists(): return (0,0,0)
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    out=[]
    i=0
    pinned=regrouped=dropped=0
    while i<len(lines):
        if not lines[i].startswith('#EXTINF:'):
            out.append(lines[i]); i+=1; continue

        start=i
        i+=1
        while i<len(lines) and not lines[i].startswith('#EXTINF:'):
            i+=1
        chunk=lines[start:i]
        name=display(chunk[0]) or ''

        if name in DROP_EXACT:
            dropped+=1
            continue

        group=REGROUP.get(name)
        if group and f'group-title="{group}"' not in chunk[0]:
            chunk[0]=set_group(chunk[0],group)
            regrouped+=1

        replacement=VERIFIED.get(name)
        if replacement:
            for j in range(1,len(chunk)):
                if chunk[j].strip() and not chunk[j].lstrip().startswith('#'):
                    old=chunk[j].rstrip('\r\n')
                    if old!=replacement:
                        ending='\r\n' if chunk[j].endswith('\r\n') else '\n'
                        chunk[j]=replacement+ending
                        pinned+=1
                    break
        out.extend(chunk)

    if pinned or regrouped or dropped:
        path.write_text(''.join(out),encoding='utf-8',newline='')
    return pinned,regrouped,dropped

def main()->int:
    tp=tr=td=0
    for p in PLAYLISTS:
        pinned,regrouped,dropped=patch(p)
        tp+=pinned; tr+=regrouped; td+=dropped
        print(f'{p}: pinned={pinned} regrouped={regrouped} dropped={dropped}')
    print(f'total_pinned={tp} total_regrouped={tr} total_dropped={td}')
    return 0
if __name__=='__main__': raise SystemExit(main())
