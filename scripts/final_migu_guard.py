#!/usr/bin/env python3
"""Final guard after the full Migu catalog merge.

This intentionally runs LAST. It protects exact channel identity from fuzzy
catalog matching, keeps non-satellite specialty services out of 卫视台, removes
semantic duplicates, and filters movie/documentary-style additions that do not
belong in the user's curated domestic list. Migu playback itself is user-verified,
so there is no speed gate here.
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

PLAYLISTS=(Path('tv-easy.m3u'),Path('tv.m3u'),Path('tv-all.m3u'),Path('tv-core.m3u'))
CCTV_SOURCE='https://raw.githubusercontent.com/ioptu/migu_video/main/cctv.migu.m3u'
UA='Mozilla/5.0 (AppleTV; APTV final Migu identity guard/1.1)'

CCTV_EXACT={
    'CCTV5+':'CCTV-5+',
    'CCTV6':'CCTV-6',
    'CCTV8':'CCTV-8',
}

GROUP_FIX={
    'CCTV4欧洲':'中文综合',
    'CCTV4美洲':'中文综合',
    '中学生':'中文付费',
}

# Same service under two labels. Preserve the long-standing display name.
DUPLICATE_ALIASES=(('农林卫视','中国农林卫视'),)

# Do not let a full-catalog merge quietly reintroduce movie/documentary channels.
DROP_UNWANTED={
    '上视东方影视',
    '南方影视',
    '江苏影视频道',
    '发现之旅',
    '老故事',
}


def display(line:str)->str|None:
    raw=line.rstrip('\r\n')
    if not raw.startswith('#EXTINF:') or ',' not in raw:
        return None
    return raw.rsplit(',',1)[1].strip()


def set_group(line:str,group:str)->str:
    ending='\r\n' if line.endswith('\r\n') else '\n'
    raw=line.rstrip('\r\n')
    if re.search(r'group-title="[^"]*"',raw):
        raw=re.sub(r'group-title="[^"]*"',f'group-title="{group}"',raw,count=1)
    else:
        comma=raw.rfind(',')
        if comma>=0:
            raw=raw[:comma]+f' group-title="{group}"'+raw[comma:]
    return raw+ending


def fetch_cctv_exact()->dict[str,str]:
    req=urllib.request.Request(CCTV_SOURCE,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=18) as r:
        text=r.read().decode('utf-8','replace')
    wanted=set(CCTV_EXACT)
    found:dict[str,str]={}
    lines=text.splitlines()
    for i,line in enumerate(lines):
        name=display(line)
        if name not in wanted:
            continue
        j=i+1
        while j<len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith('#')):
            j+=1
        if j<len(lines) and lines[j].startswith(('http://','https://')):
            found[CCTV_EXACT[name]]=lines[j].strip()
    missing=set(CCTV_EXACT.values())-set(found)
    if missing:
        raise RuntimeError('missing dedicated CCTV entries: '+','.join(sorted(missing)))
    return found


def chunks(lines:list[str])->list[list[str]]:
    out:list[list[str]]=[]
    prefix:list[str]=[]
    i=0
    while i<len(lines) and not lines[i].startswith('#EXTINF:'):
        prefix.append(lines[i]); i+=1
    if prefix:
        out.append(prefix)
    while i<len(lines):
        start=i; i+=1
        while i<len(lines) and not lines[i].startswith('#EXTINF:'):
            i+=1
        out.append(lines[start:i])
    return out


def update_count(text:str)->str:
    count=sum(1 for x in text.splitlines() if x.startswith('#EXTINF:'))
    if re.search(r'(?m)^# channels=\d+\s*$',text):
        text=re.sub(r'(?m)^# channels=\d+\s*$',f'# channels={count}',text,count=1)
    return text


def patch(path:Path,exact:dict[str,str])->tuple[int,int,int]:
    if not path.exists():
        return 0,0,0
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    parts=chunks(lines)
    names={display(c[0]) for c in parts if c and c[0].startswith('#EXTINF:')}
    drop=set(DROP_UNWANTED)
    for keep,duplicate in DUPLICATE_ALIASES:
        if keep in names and duplicate in names:
            drop.add(duplicate)

    out:list[str]=[]
    identities=groups=dropped=0
    for c in parts:
        if not c or not c[0].startswith('#EXTINF:'):
            out.extend(c); continue
        name=display(c[0]) or ''
        if name in drop:
            dropped+=1
            print(f'{path}:{name}: drop curated-list exclusion/duplicate')
            continue
        if name in GROUP_FIX and f'group-title="{GROUP_FIX[name]}"' not in c[0]:
            c[0]=set_group(c[0],GROUP_FIX[name])
            groups+=1
            print(f'{path}:{name}: group -> {GROUP_FIX[name]}')
        wanted=exact.get(name)
        if wanted:
            for j in range(1,len(c)):
                if c[j].strip() and not c[j].lstrip().startswith('#'):
                    old=c[j].rstrip('\r\n')
                    if old!=wanted:
                        ending='\r\n' if c[j].endswith('\r\n') else '\n'
                        c[j]=wanted+ending
                        identities+=1
                        print(f'{path}:{name}: dedicated CCTV identity corrected')
                    break
        out.extend(c)

    text=''.join(out)
    if dropped:
        text=update_count(text)
    if identities or groups or dropped:
        path.write_text(text,encoding='utf-8',newline='')
    return identities,groups,dropped


def main()->int:
    exact=fetch_cctv_exact()
    print('dedicated_cctv_resolved='+','.join(sorted(exact)))
    ti=tg=td=0
    for p in PLAYLISTS:
        i,g,d=patch(p,exact)
        ti+=i; tg+=g; td+=d
        print(f'{p}: identity={i} group={g} dropped={d}')
    print(f'total_identity={ti} total_group={tg} total_dropped={td}')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
