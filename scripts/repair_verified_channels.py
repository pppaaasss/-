#!/usr/bin/env python3
"""Surgical post-build repair for domestic TV identities and quality.

Migu handling is based on the user's real viewing line:
- Migu signed streams are user-verified playable, so there is no GitHub speed gate.
- lemonTV is built from TWO upstream lists: migu.m3u and cctv.migu.m3u.
- Those lists use different display names (CCTV1综合 vs CCTV1, CCTV6电影 vs
  CCTV6, etc.), so CCTV is matched by canonical channel number, never by the
  visible name.
- The dedicated cctv.migu.m3u (lemonTV's "央视专版") wins over the general
  Migu copy when both provide the same CCTV channel.
- Existing measured high-bitrate public routes are retained when their quality
  tier is clearly higher.
"""
from __future__ import annotations
import re
import urllib.request
from pathlib import Path

PLAYLISTS=(Path('tv-easy.m3u'),Path('tv.m3u'),Path('tv-all.m3u'),Path('tv-core.m3u'))
MIGU_GENERAL_SOURCE='https://raw.githubusercontent.com/ioptu/migu_video/main/migu.m3u'
MIGU_CCTV_SOURCE='https://raw.githubusercontent.com/ioptu/migu_video/main/cctv.migu.m3u'
MIGU_MERGED_FALLBACK='https://raw.githubusercontent.com/jia070310/lemonTV/main/iptv-fe.m3u'
UA='Mozilla/5.0 (AppleTV; APTV domestic repair/1.2)'

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

# These three are current repair targets: correct identity + user-confirmed Migu
# playback matters more than GitHub-hosted speed tests.
MIGU_FORCE={'CCTV-5+','CCTV-6','CCTV-8'}

REGROUP={'央视文化精品':'中文付费','人间卫视':'台湾'}
DROP_EXACT={
    'CCTV央视台球','星空卫视','北京衛視 (1080p) [Geo-blocked]','康巴卫视',
    '央视网-云南保山瓦窑镇瓦窑码头','央视网-云南普洱澜沧县景迈山',
    '央视网-云南西双版纳千年古寨曼丢','央视网-安徽黄山始信新道',
    '央视网-江苏无锡鼋头渚','央视网-河北邯郸京娘湖',
    '央视网-西藏日喀则珠穆朗玛峰','央视网-西藏林芝岷山错高民宿',
    '央视网-西藏林芝鲁朗色季拉山',
}
FORBIDDEN_BY_NAME={'CCTV-8':('cctv8k',),'黑龙江卫视':('SXyuyao3',)}

def display(line:str)->str|None:
    line=line.rstrip('\r\n')
    if not line.startswith('#EXTINF:') or ',' not in line: return None
    return line.rsplit(',',1)[1].strip()

def group_of(line:str)->str:
    m=re.search(r'group-title="([^"]*)"',line)
    return m.group(1) if m else ''

def canonical_cctv(name:str)->str|None:
    """Normalize both Migu naming systems to our CCTV-N display names."""
    s=name.upper().replace(' ','').replace('－','-')
    # Do not collapse international CCTV4 variants into domestic CCTV-4.
    if '欧洲' in name or '美洲' in name:
        return None
    if s.startswith('CCTV5+'):
        return 'CCTV-5+'
    if s.startswith('CCTV4K'):
        return 'CCTV-4K'
    m=re.match(r'^CCTV-?(\d{1,2})(?:\D|$)',s)
    if not m:
        return None
    n=int(m.group(1))
    if 1<=n<=17:
        return f'CCTV-{n}'
    return None

def fetch_text(url:str)->str:
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=15) as r:
        return r.read().decode('utf-8','replace')

def parse_m3u(text:str, source:str)->dict[str,tuple[str,str,str]]:
    """canonical -> (url, source, original display name)."""
    out:dict[str,tuple[str,str,str]]={}
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if not line.startswith('#EXTINF:'):
            continue
        src_name=display(line) or ''
        canonical=canonical_cctv(src_name)
        if canonical is None and source=='general' and group_of(line)=='卫视' and src_name.endswith('卫视'):
            canonical=src_name
        if not canonical:
            continue
        j=i+1
        while j<len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith('#')):
            j+=1
        if j<len(lines) and lines[j].startswith(('http://','https://')):
            out[canonical]=(lines[j].strip(),source,src_name)
    return out

def fetch_migu()->dict[str,tuple[str,str,str]]:
    """Read both Migu lists separately; dedicated CCTV overrides general CCTV."""
    combined:dict[str,tuple[str,str,str]]={}
    general_ok=cctv_ok=False
    try:
        general=parse_m3u(fetch_text(MIGU_GENERAL_SOURCE),'general')
        combined.update(general); general_ok=True
        print(f'migu_general_resolved={len(general)}')
    except Exception as exc:
        print(f'migu_general_fetch_failed={type(exc).__name__}:{exc}')
    try:
        dedicated=parse_m3u(fetch_text(MIGU_CCTV_SOURCE),'cctv-special')
        # This is the second naming set / 央视专版. It deliberately overrides
        # only canonical CCTV keys; satellite routes remain from general.
        combined.update(dedicated); cctv_ok=True
        print(f'migu_cctv_special_resolved={len(dedicated)}')
    except Exception as exc:
        print(f'migu_cctv_fetch_failed={type(exc).__name__}:{exc}')
    if not general_ok and not cctv_ok:
        try:
            merged=parse_m3u(fetch_text(MIGU_MERGED_FALLBACK),'general')
            combined.update(merged)
            print(f'migu_merged_fallback_resolved={len(merged)}')
        except Exception as exc:
            print(f'migu_merged_fallback_failed={type(exc).__name__}:{exc}')
    return combined

def url_quality_kbps(url:str, channel:str='')->int:
    """Quality-tier hint only; it is NOT a Migu speed test."""
    low=url.lower()
    if channel in VERIFIED_KBPS and url==VERIFIED.get(channel):
        return VERIFIED_KBPS[channel]
    # Migu profile number before index.m3u8 (e.g. /1200/, /1500/, /2000/).
    m=re.search(r'/([5-9]\d\d|[1-9]\d{3,4})/index\.m3u8',low)
    if m:
        n=int(m.group(1))
        if 500<=n<=20000: return n
    for pat,value in ((r'(?<!\d)10m(?:-|/|1080p)',10000),(r'(?<!\d)8m(?:-|/|1080p)',8000),
                      (r'(?<!\d)6m(?:-|/|1080p)',6000),(r'(?<!\d)4m(?:-|/|1080p)',4000),
                      (r'(?<!\d)3m1080p',3000)):
        if re.search(pat,low): return value
    if '8k' in low: return 20000
    if '4k' in low or 'uhd' in low: return 12000
    if '1080p50' in low or 'hd8m' in low: return 8000
    if '1080' in low: return 3000
    if 'hd' in low or 'wshd' in low: return 2400
    if any(x in low for x in ('sd/','/sd/','576p','480p')): return 700
    return 1300

def set_group(extinf:str, group:str)->str:
    ending='\r\n' if extinf.endswith('\r\n') else '\n'
    raw=extinf.rstrip('\r\n')
    if re.search(r'group-title="[^"]*"',raw):
        raw=re.sub(r'group-title="[^"]*"',f'group-title="{group}"',raw,count=1)
    else:
        comma=raw.rfind(',')
        if comma>=0: raw=raw[:comma]+f' group-title="{group}"'+raw[comma:]
    return raw+ending

def choose_route(name:str,current:str,migu:dict[str,tuple[str,str,str]])->tuple[str,str]:
    base=VERIFIED.get(name,current)
    row=migu.get(name)
    if not row:
        return base,'verified/current'
    candidate,source,src_name=row
    if name in MIGU_FORCE:
        return candidate,f'{source}-forced ({src_name})'
    cq=url_quality_kbps(base,name)
    mq=url_quality_kbps(candidate,name)
    if mq>=cq+400:
        return candidate,f'{source}-quality {mq}>{cq} ({src_name})'
    return base,f'keep-quality {cq}>={mq} vs {source} ({src_name})'

def patch(path:Path,migu:dict[str,tuple[str,str,str]])->tuple[int,int,int,int]:
    if not path.exists(): return (0,0,0,0)
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    out=[]; i=0; changed=regrouped=dropped=migu_used=0
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
                    replacement=VERIFIED.get(name,current); reason='forbidden-identity-fallback'
                if replacement!=current:
                    ending='\r\n' if chunk[j].endswith('\r\n') else '\n'
                    chunk[j]=replacement+ending; changed+=1
                    row=migu.get(name)
                    if row and replacement==row[0]: migu_used+=1
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
