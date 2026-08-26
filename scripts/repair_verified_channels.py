#!/usr/bin/env python3
"""Post-build repair + full Migu catalog merge for the user's TV playlists.

Rules:
- Migu signed streams are user-verified playable on the real viewing line, so
  Migu is never rejected by GitHub-hosted speed tests.
- Read BOTH upstream Migu lists separately. cctv.migu.m3u is the dedicated
  CCTV set and overrides the general Migu copy for the same CCTV number.
- Match CCTV by canonical number, not display name (CCTV6电影 == CCTV6).
- For channels already present, compare apparent quality tiers and only replace
  when Migu is clearly better, except current identity-repair targets.
- Add genuinely missing Chinese linear-TV channels from Migu to tv.m3u and
  tv-all.m3u only. Do not bloat tv-easy/tv-core.
- Skip English/CGTN, obvious event/round-the-clock loop feeds, movie/news/
  documentary specialty additions, and other temporary/non-linear junk.
"""
from __future__ import annotations

import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PLAYLISTS=(Path('tv-easy.m3u'),Path('tv.m3u'),Path('tv-all.m3u'),Path('tv-core.m3u'))
EXPAND_PLAYLISTS={Path('tv.m3u'),Path('tv-all.m3u')}
REPORT=Path('migu-catalog-report.txt')
MIGU_GENERAL_SOURCE='https://raw.githubusercontent.com/ioptu/migu_video/main/migu.m3u'
MIGU_CCTV_SOURCE='https://raw.githubusercontent.com/ioptu/migu_video/main/cctv.migu.m3u'
MIGU_MERGED_FALLBACK='https://raw.githubusercontent.com/jia070310/lemonTV/main/iptv-fe.m3u'
UA='Mozilla/5.0 (AppleTV; APTV Migu catalog merge/1.3)'

VERIFIED={
    'CCTV-5':'http://1.24.39.180:9003/hls/5/index.m3u8',
    '湖南卫视':'http://192.151.150.154/live/hnwshd.m3u8',
    '黑龙江卫视':'http://107.150.60.122/live/hljwshd.m3u8',
    '海南卫视':'http://107.150.60.122/live/lywshd.m3u8',
    '陕西卫视':'http://107.150.60.122/live/snwshd.m3u8',
    '云南卫视':'http://107.150.60.122/live/ynwshd.m3u8',
}
VERIFIED_KBPS={
    'CCTV-5':6710,'湖南卫视':3200,'黑龙江卫视':3200,
    '海南卫视':3200,'陕西卫视':3370,'云南卫视':3380,
}
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

# These are for NEW additions only. Existing channels are never removed merely
# because their name matches one of these words.
ADD_DENY_WORDS=(
    'CGTN','英语','英文','ENGLISH','轮播','赛事轮播','城市联赛','测试',
    '电影','影迷','纪录','纪实','新闻',
)
REGION_GROUPS=(
    '北京','上海','天津','重庆','广东','广西','浙江','江苏','安徽','福建','江西',
    '山东','河南','河北','湖北','湖南','四川','贵州','云南','陕西','山西','辽宁',
    '吉林','黑龙江','内蒙古','宁夏','甘肃','青海','新疆','西藏','海南',
)

@dataclass
class Entry:
    key:str
    name:str
    tvg_id:str
    logo:str
    group:str
    url:str
    source:str


def display(line:str)->str|None:
    line=line.rstrip('\r\n')
    if not line.startswith('#EXTINF:') or ',' not in line:
        return None
    return line.rsplit(',',1)[1].strip()


def attr(line:str,name:str)->str:
    m=re.search(rf'{re.escape(name)}="([^"]*)"',line)
    return m.group(1) if m else ''


def group_of(line:str)->str:
    return attr(line,'group-title')


def canonical_cctv(name:str)->str|None:
    s=name.upper().replace(' ','').replace('－','-')
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
    return f'CCTV-{n}' if 1<=n<=17 else None


def generic_key(name:str)->str:
    c=canonical_cctv(name)
    if c:
        return c
    # Conservative normalization only: punctuation/spacing variants, not
    # semantic suffix stripping that could collapse a 4K service into an HD one.
    s=name.strip().replace('　',' ').replace('－','-')
    s=re.sub(r'\s+','',s)
    return s.casefold()


def fetch_text(url:str)->str:
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=18) as r:
        return r.read().decode('utf-8','replace')


def url_quality_kbps(url:str, channel:str='')->int:
    """Quality-profile hint only; NOT a Migu speed test."""
    low=url.lower()
    if channel in VERIFIED_KBPS and url==VERIFIED.get(channel):
        return VERIFIED_KBPS[channel]
    m=re.search(r'/([5-9]\d\d|[1-9]\d{3,4})/index\.m3u8',low)
    if m:
        n=int(m.group(1))
        if 500<=n<=20000:
            return n
    for pat,value in (
        (r'(?<!\d)10m(?:-|/|1080p)',10000),(r'(?<!\d)8m(?:-|/|1080p)',8000),
        (r'(?<!\d)6m(?:-|/|1080p)',6000),(r'(?<!\d)4m(?:-|/|1080p)',4000),
        (r'(?<!\d)3m1080p',3000),
    ):
        if re.search(pat,low):
            return value
    if '8k' in low: return 20000
    if '4k' in low or 'uhd' in low: return 12000
    if '1080p50' in low or 'hd8m' in low: return 8000
    if '1080' in low: return 3000
    if 'hd' in low or 'wshd' in low: return 2400
    if any(x in low for x in ('sd/','/sd/','576p','480p')): return 700
    return 1300


def parse_m3u(text:str,source:str,all_general:bool=False)->dict[str,Entry]:
    out:dict[str,Entry]={}
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if not line.startswith('#EXTINF:'):
            continue
        name=display(line) or ''
        group=group_of(line)
        cctv=canonical_cctv(name)
        if source=='cctv-special' and not cctv:
            continue
        if source=='general' and not all_general:
            if not cctv and not (group=='卫视' and name.endswith('卫视')):
                continue
        key=cctv or generic_key(name)
        if not key:
            continue
        j=i+1
        while j<len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith('#')):
            j+=1
        if j>=len(lines) or not lines[j].startswith(('http://','https://')):
            continue
        e=Entry(
            key=key,name=name,tvg_id=attr(line,'tvg-id') or name,
            logo=attr(line,'tvg-logo'),group=group,url=lines[j].strip(),source=source,
        )
        old=out.get(key)
        if old is None or url_quality_kbps(e.url,name)>url_quality_kbps(old.url,old.name):
            out[key]=e
    return out


def fetch_migu()->dict[str,Entry]:
    """Full general catalog + dedicated CCTV override."""
    combined:dict[str,Entry]={}
    general_ok=cctv_ok=False
    try:
        general=parse_m3u(fetch_text(MIGU_GENERAL_SOURCE),'general',all_general=True)
        combined.update(general); general_ok=True
        print(f'migu_general_catalog={len(general)}')
    except Exception as exc:
        print(f'migu_general_fetch_failed={type(exc).__name__}:{exc}')
    try:
        dedicated=parse_m3u(fetch_text(MIGU_CCTV_SOURCE),'cctv-special',all_general=True)
        combined.update(dedicated); cctv_ok=True
        print(f'migu_cctv_special={len(dedicated)}')
    except Exception as exc:
        print(f'migu_cctv_fetch_failed={type(exc).__name__}:{exc}')
    if not general_ok and not cctv_ok:
        try:
            merged=parse_m3u(fetch_text(MIGU_MERGED_FALLBACK),'general',all_general=True)
            combined.update(merged)
            print(f'migu_merged_fallback={len(merged)}')
        except Exception as exc:
            print(f'migu_merged_fallback_failed={type(exc).__name__}:{exc}')
    return combined


def set_group(extinf:str,group:str)->str:
    ending='\r\n' if extinf.endswith('\r\n') else '\n'
    raw=extinf.rstrip('\r\n')
    if re.search(r'group-title="[^"]*"',raw):
        raw=re.sub(r'group-title="[^"]*"',f'group-title="{group}"',raw,count=1)
    else:
        comma=raw.rfind(',')
        if comma>=0:
            raw=raw[:comma]+f' group-title="{group}"'+raw[comma:]
    return raw+ending


def target_group(e:Entry)->str:
    if canonical_cctv(e.name) or e.group in {'央视','卫视'} or e.name.endswith('卫视'):
        return '卫视台'
    if re.match(r'^CETV\d+$',e.name,re.I):
        return '中文综合'
    for region in REGION_GROUPS:
        if e.name.startswith(region):
            return region
    return '中文综合'


def addable(e:Entry)->tuple[bool,str]:
    name=e.name
    upper=name.upper()
    if e.group not in {'央视','卫视','其它'}:
        return False,'group-not-selected'
    if any(w.upper() in upper for w in ADD_DENY_WORDS):
        return False,'deny-word'
    if '/lunbo/' in e.url.lower():
        return False,'loop-feed'
    # No English-only channels. CETV is a Chinese broadcaster acronym and is
    # explicitly allowed.
    if re.fullmatch(r'[A-Z0-9+_. -]+',upper) and not re.fullmatch(r'CETV\d+',upper):
        return False,'latin-only'
    # In the catch-all group, only accept feeds that look like linear channels.
    if e.group=='其它':
        low=e.url.lower()
        if not (re.match(r'^CETV\d+$',name,re.I) or '/kailu/' in low or '/wd_' in low or '/envivo' in low):
            return False,'other-not-linear-looking'
    return True,'ok'


def choose_route(name:str,current:str,migu:dict[str,Entry])->tuple[str,str]:
    key=generic_key(name)
    base=VERIFIED.get(name,current)
    row=migu.get(key)
    if not row:
        return base,'verified/current'
    if name in MIGU_FORCE:
        return row.url,f'{row.source}-forced ({row.name})'
    cq=url_quality_kbps(base,name)
    mq=url_quality_kbps(row.url,row.name)
    if mq>=cq+400:
        return row.url,f'{row.source}-quality {mq}>{cq} ({row.name})'
    return base,f'keep-quality {cq}>={mq} vs {row.source} ({row.name})'


def extinf_for(e:Entry)->str:
    group=target_group(e)
    attrs=[f'tvg-id="{e.tvg_id}"',f'tvg-name="{e.name}"']
    if e.logo:
        attrs.append(f'tvg-logo="{e.logo}"')
    attrs.append(f'group-title="{group}"')
    return '#EXTINF:-1 '+' '.join(attrs)+f',{e.name}\n'


def update_count_header(text:str)->str:
    count=sum(1 for line in text.splitlines() if line.startswith('#EXTINF:'))
    if re.search(r'(?m)^# channels=\d+\s*$',text):
        return re.sub(r'(?m)^# channels=\d+\s*$',f'# channels={count}',text,count=1)
    return text


def patch(path:Path,migu:dict[str,Entry],report:list[str])->tuple[int,int,int,int,int]:
    if not path.exists():
        return (0,0,0,0,0)
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    out=[]; i=0
    changed=regrouped=dropped=migu_used=added=0
    present:set[str]=set()

    while i<len(lines):
        if not lines[i].startswith('#EXTINF:'):
            out.append(lines[i]); i+=1; continue
        start=i; i+=1
        while i<len(lines) and not lines[i].startswith('#EXTINF:'):
            i+=1
        chunk=lines[start:i]
        name=display(chunk[0]) or ''
        key=generic_key(name)
        present.add(key)
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
                    row=migu.get(key)
                    if row and replacement==row.url:
                        migu_used+=1
                    report.append(f'UPGRADE\t{path}\t{name}\t{reason}')
                    print(f'{path}:{name}: {reason}')
                break
        out.extend(chunk)

    if path in EXPAND_PLAYLISTS:
        additions=[]
        for key,e in sorted(migu.items(),key=lambda kv:(target_group(kv[1]),kv[1].name)):
            if key in present:
                continue
            ok,why=addable(e)
            if not ok:
                report.append(f'SKIP_NEW\t{path}\t{e.name}\t{why}\t{e.group}')
                continue
            additions.extend([extinf_for(e),e.url+'\n'])
            present.add(key); added+=1; migu_used+=1
            report.append(f'ADD\t{path}\t{e.name}\tgroup={target_group(e)}\tsource={e.source}\tq={url_quality_kbps(e.url,e.name)}')
        if additions:
            if out and not out[-1].endswith('\n'):
                out[-1]+='\n'
            out.extend(additions)

    text=''.join(out)
    if path in EXPAND_PLAYLISTS:
        text=update_count_header(text)
    if changed or regrouped or dropped or added:
        path.write_text(text,encoding='utf-8',newline='')
    return changed,regrouped,dropped,migu_used,added


def main()->int:
    migu=fetch_migu()
    report=[
        f'generated_utc={time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}',
        f'migu_catalog_entries={len(migu)}',
        'policy=compare overlaps; add missing Chinese linear TV only to tv.m3u/tv-all.m3u; Migu speed exempt',
        '',
    ]
    tc=tr=td=tm=ta=0
    for p in PLAYLISTS:
        changed,regrouped,dropped,migu_used,added=patch(p,migu,report)
        tc+=changed; tr+=regrouped; td+=dropped; tm+=migu_used; ta+=added
        print(f'{p}: changed={changed} added={added} migu_used={migu_used} regrouped={regrouped} dropped={dropped}')
    report.insert(3,f'summary_changed={tc} summary_added={ta} summary_migu_used={tm} summary_regrouped={tr} summary_dropped={td}')
    REPORT.write_text('\n'.join(report)+'\n',encoding='utf-8')
    print(f'total_changed={tc} total_added={ta} total_migu_used={tm} total_regrouped={tr} total_dropped={td}')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
