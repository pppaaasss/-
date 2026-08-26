#!/usr/bin/env python3
"""Surgical post-build repair for domestic TV identities and quality.

Policy for the user's actual viewing line:
- lemonTV Migu signed streams are user-verified playable, so they are NOT
  rejected by GitHub-hosted download-speed tests.
- For the same domestic channel, compare the apparent quality tier of the
  current URL and the fresh Migu URL. Use Migu only when its tier is clearly
  higher, except for a few identity/quality repairs that are forced to Migu.
- Keep measured high-bitrate public routes when they are better.
- Exact-name regroup/drop rules remain surgical; unrelated channels are not
  reordered or rewritten.
"""
from __future__ import annotations
import re
import urllib.request
from pathlib import Path

PLAYLISTS=(Path('tv-easy.m3u'),Path('tv.m3u'),Path('tv-all.m3u'),Path('tv-core.m3u'))
MIGU_SOURCE='https://raw.githubusercontent.com/jia070310/lemonTV/main/iptv-fe.m3u'
UA='Mozilla/5.0 (AppleTV; APTV domestic repair/1.1)'

# Fixed routes with known measured quality. These are still allowed to lose to
# Migu if a Migu profile is genuinely higher, but their measured quality hints
# prevent a lower-bitrate Migu feed from replacing them just for uniformity.
VERIFIED={
    'CCTV-5':'http://1.24.39.180:9003/hls/5/index.m3u8',
    '湖南卫视':'http://192.151.150.154/live/hnwshd.m3u8',
    '黑龙江卫视':'http://107.150.60.122/live/hljwshd.m3u8',
    '海南卫视':'http://107.150.60.122/live/lywshd.m3u8',
    '陕西卫视':'http://107.150.60.122/live/snwshd.m3u8',
    '云南卫视':'http://107.150.60.122/live/ynwshd.m3u8',
}
VERIFIED_KBPS={
    'CCTV-5':6710,
    '湖南卫视':3200,
    '黑龙江卫视':3200,
    '海南卫视':3200,
    '陕西卫视':3370,
    '云南卫视':3380,
}

# lemonTV names -> our canonical display names. Satellite names are normally
# identical and are collected automatically, while CCTV names need aliases.
CCTV_MIGU_ALIASES={
    'CCTV1综合':'CCTV-1',
    'CCTV2财经':'CCTV-2',
    'CCTV3综艺':'CCTV-3',
    'CCTV4中文国际':'CCTV-4',
    'CCTV5体育':'CCTV-5',
    'CCTV5+体育赛事':'CCTV-5+',
    'CCTV6电影':'CCTV-6',
    'CCTV7国防军事':'CCTV-7',
    'CCTV8电视剧':'CCTV-8',
    'CCTV9纪录':'CCTV-9',
    'CCTV10科教':'CCTV-10',
    'CCTV11戏曲':'CCTV-11',
    'CCTV12社会与法':'CCTV-12',
    'CCTV13新闻':'CCTV-13',
    'CCTV14少儿':'CCTV-14',
    'CCTV15音乐':'CCTV-15',
    'CCTV16奥林匹克':'CCTV-16',
    'CCTV17农业农村':'CCTV-17',
}

# User has already verified these Migu channels on the real viewing network;
# they fix current low-bitrate/wrong-identity cases and therefore do not need a
# GitHub speed gate or a quality-margin gate.
MIGU_FORCE={'CCTV-5+','CCTV-6','CCTV-8'}

REGROUP={
    '央视文化精品':'中文付费',
    '人间卫视':'台湾',
}

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

FORBIDDEN_BY_NAME={
    'CCTV-8':('cctv8k',),
    '黑龙江卫视':('SXyuyao3',),
}

def display(line:str)->str|None:
    line=line.rstrip('\r\n')
    if not line.startswith('#EXTINF:') or ',' not in line: return None
    return line.rsplit(',',1)[1].strip()

def group_of(line:str)->str:
    m=re.search(r'group-title="([^"]*)"',line)
    return m.group(1) if m else ''

def url_quality_kbps(url:str, channel:str='')->int:
    """Conservative quality hint from route/profile naming, never speed.

    Migu profile paths such as /2000/index.m3u8 are treated as kbps tiers.
    Common public-source naming (8m/10m/3m1080p/4k/hd) gets a conservative
    estimate. Unknown routes stay low enough that a clearly higher Migu HD tier
    can win, but ordinary 1000/1200 Migu feeds do not displace known HD routes.
    """
    low=url.lower()
    if channel in VERIFIED_KBPS and url==VERIFIED.get(channel):
        return VERIFIED_KBPS[channel]
    # Explicit bitrate markers are strongest.
    for pat,mult in ((r'(?<!\d)(10)m(?:1080p)?',1000),(r'(?<!\d)8m(?:-|/|1080p)',1000),
                     (r'(?<!\d)6m(?:-|/|1080p)',1000),(r'(?<!\d)4m(?:-|/|1080p)',1000),
                     (r'(?<!\d)3m1080p',1000)):
        m=re.search(pat,low)
        if m:
            try: return int(m.group(1))*mult
            except Exception: pass
    # Migu numeric profile immediately before index.m3u8.
    m=re.search(r'/([5-9]\d\d|[1-9]\d{3,4})/index\.m3u8',low)
    if m:
        n=int(m.group(1))
        if 500<=n<=20000: return n
    if '8k' in low: return 20000
    if '4k' in low or 'uhd' in low: return 12000
    if '1080p50' in low or 'hd8m' in low: return 8000
    if '1080' in low: return 3000
    if 'hd' in low or 'wshd' in low: return 2400
    if any(x in low for x in ('sd/','/sd/','576p','480p')): return 700
    return 1300

def fetch_migu()->dict[str,str]:
    """Resolve fresh Migu signed URLs for CCTV + mainland satellite channels."""
    out:dict[str,str]={}
    try:
        req=urllib.request.Request(MIGU_SOURCE,headers={'User-Agent':UA})
        with urllib.request.urlopen(req,timeout=15) as r:
            text=r.read().decode('utf-8','replace')
    except Exception as exc:
        print(f'migu_fetch_failed={type(exc).__name__}:{exc}')
        return out
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if not line.startswith('#EXTINF:'):
            continue
        src_name=display(line) or ''
        src_group=group_of(line)
        canonical=CCTV_MIGU_ALIASES.get(src_name)
        if canonical is None and src_group=='卫视' and src_name.endswith('卫视'):
            canonical=src_name
        if not canonical:
            continue
        j=i+1
        while j<len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith('#')):
            j+=1
        if j<len(lines) and lines[j].startswith(('http://','https://')):
            out[canonical]=lines[j].strip()
    print(f'migu_resolved_channels={len(out)}')
    return out

def set_group(extinf:str, group:str)->str:
    ending='\r\n' if extinf.endswith('\r\n') else '\n'
    raw=extinf.rstrip('\r\n')
    if re.search(r'group-title="[^"]*"',raw):
        raw=re.sub(r'group-title="[^"]*"',f'group-title="{group}"',raw,count=1)
    else:
        comma=raw.rfind(',')
        if comma>=0: raw=raw[:comma]+f' group-title="{group}"'+raw[comma:]
    return raw+ending

def choose_route(name:str,current:str,migu:dict[str,str])->tuple[str,str]:
    # Start from a measured fixed route when one exists.
    base=VERIFIED.get(name,current)
    candidate=migu.get(name)
    if not candidate:
        return base,'verified/current'
    if name in MIGU_FORCE:
        return candidate,'migu-forced-user-verified'
    cq=url_quality_kbps(base,name)
    mq=url_quality_kbps(candidate,name)
    # A real improvement must be visible, not a 100 kbps coin flip.
    if mq>=cq+400:
        return candidate,f'migu-quality {mq}>{cq}'
    return base,f'keep-quality {cq}>={mq}'

def patch(path:Path, migu:dict[str,str])->tuple[int,int,int,int]:
    if not path.exists(): return (0,0,0,0)
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    out=[]; i=0
    changed=regrouped=dropped=migu_used=0
    while i<len(lines):
        if not lines[i].startswith('#EXTINF:'):
            out.append(lines[i]); i+=1; continue
        start=i; i+=1
        while i<len(lines) and not lines[i].startswith('#EXTINF:'): i+=1
        chunk=lines[start:i]
        name=display(chunk[0]) or ''
        if name in DROP_EXACT:
            dropped+=1; continue
        group=REGROUP.get(name)
        if group and f'group-title="{group}"' not in chunk[0]:
            chunk[0]=set_group(chunk[0],group); regrouped+=1
        for j in range(1,len(chunk)):
            if chunk[j].strip() and not chunk[j].lstrip().startswith('#'):
                current=chunk[j].rstrip('\r\n')
                replacement,reason=choose_route(name,current,migu)
                forbidden=FORBIDDEN_BY_NAME.get(name,())
                if any(x.lower() in replacement.lower() for x in forbidden):
                    replacement=VERIFIED.get(name,current)
                    reason='forbidden-identity-fallback'
                if replacement!=current:
                    ending='\r\n' if chunk[j].endswith('\r\n') else '\n'
                    chunk[j]=replacement+ending
                    changed+=1
                    if replacement==migu.get(name): migu_used+=1
                    print(f'{path}:{name}: {reason}')
                break
        out.extend(chunk)
    if changed or regrouped or dropped:
        path.write_text(''.join(out),encoding='utf-8',newline='')
    return changed,regrouped,dropped,migu_used

def main()->int:
    migu=fetch_migu()
    tc=tr=td=tm=0
    for p in PLAYLISTS:
        changed,regrouped,dropped,migu_used=patch(p,migu)
        tc+=changed; tr+=regrouped; td+=dropped; tm+=migu_used
        print(f'{p}: changed={changed} migu_used={migu_used} regrouped={regrouped} dropped={dropped}')
    print(f'total_changed={tc} total_migu_used={tm} total_regrouped={tr} total_dropped={td}')
    return 0
if __name__=='__main__': raise SystemExit(main())
