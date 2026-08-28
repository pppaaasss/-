#!/usr/bin/env python3
from __future__ import annotations

import json,re,time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from channel_regions import MAINLAND_REGION_GROUPS, OUTPUT_GROUP_ORDER, regionalized_group

TARGET=400
SPORT_CAP=40
HAN=re.compile(r'[\u3400-\u9fff]'); KANA=re.compile(r'[\u3040-\u30ff]')
SPORT=re.compile(r'(?:体育|體育|足球|篮球|籃球|网球|網球|高尔夫|赛车|搏击|sport|espn|bein|eurosport|dazn|nba|nfl|mlb|nhl|ufc|formula\s*1|\bf1\b|motorsport|racing|golf|tennis|football|soccer|sky\s*sports|premier\s*sports|astro\s*supersport|纬来体育|緯來體育|博斯|五星体育)',re.I)
BBC=re.compile(r'(?<![a-z])bbc(?:\s|[-_]|$)',re.I)
JUNK=re.compile(r'(?:adult|xxx|porn|情色|成人|购物|購物|shopping|test\b|测试|測試|demo\b|radio\b|广播|廣播|电台|電台|weather\b|webcam|camera\b|traffic\b|免费订阅|請勿販賣|请勿贩卖|公告说明|公告說明)',re.I)
PAY=re.compile(r'(?:CCTV.*(?:风云|風雲|第一剧场|第一劇場|怀旧|懷舊|文化精品|世界地理)|CHC|NewTV|SiTV|BesTV|百视通|百視通|动作电影|動作電影|家庭影院|家庭電影|影迷电影|影迷電影|武博世界)',re.I)
KIDS=re.compile(r'(?:少儿|少兒|卡酷|优漫|優漫|嘉佳卡通|动漫|動漫|动画|動畫|金鹰卡通|金鷹卡通)',re.I)
MUSIC=re.compile(r'(?:音乐|音樂|点歌|點歌|music)',re.I)
EDU=re.compile(r'(?:教育|科教|课堂|課堂|大学|大學|考试|考試)',re.I)
FIN=re.compile(r'(?:财经|財經|证券|證券|经济|經濟|金融)',re.I)
ENT=re.compile(r'(?:电影|電影|影院|剧场|劇場|影视|影視|综艺|綜藝|戏曲|戲曲)',re.I)
BAD_HOST=('bdstatic.com','material.1989.click','goodiptv.club','metshop.top','ottiptv.cc')


def blocks(path:Path):
    lines=path.read_text(encoding='utf-8').splitlines();out=[]
    for i,line in enumerate(lines[:-1]):
        if not line.startswith('#EXTINF:') or ',' not in line:continue
        name=line.rsplit(',',1)[-1].strip();j=i+1
        while j<len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith('#')):j+=1
        if j>=len(lines):continue
        url=lines[j].strip()
        if not url.startswith(('http://','https://')):continue
        gm=re.search(r'group-title="([^"]+)"',line)
        out.append({'name':name,'extinf':line,'url':url,'group':gm.group(1) if gm else ''})
    return out

def cctv_id(s):
    x=s.casefold().replace('＋','+')
    if re.search(r'cctv\s*[-_ ]?8\s*k\b',x):return 'cctv8k'
    if re.search(r'cctv\s*[-_ ]?4\s*k\b',x):return 'cctv4k'
    if re.search(r'cctv\s*[-_ ]?5\s*(?:\+|plus)',x):return 'cctv5+'
    m=re.search(r'cctv\s*[-_ ]?(\d{1,2})(?!\s*k)',x)
    return f'cctv{int(m.group(1))}' if m else None

def canon(s):
    c=cctv_id(s)
    if c:return c
    x=s.casefold().replace('＋','+')
    x=re.sub(r'\b(?:uhd|fhd|full\s*hd|hd|sd|hevc|h265|h264|avc)\b|(?:2160|1080|720|576|540|480)[pi]?|超高清|高清|标清|標清|蓝光|藍光','',x,flags=re.I)
    return re.sub(r'[\s\-_.·•()（）\[\]【】/\\]+','',x)[:160]

def vodish(name,url):
    low=url.lower();host=(urlparse(url).hostname or '').lower()
    if '.mp4' in low:return True
    if any(x in host for x in BAD_HOST):return True
    if '/huya/' in low or '/douyu/' in low:return True
    if len(name)>34 and not re.search(r'(电视|電視|频道|頻道|卫视|衛視|TV)',name,re.I):return True
    return False

def classify(name,groups):
    text=' '.join([name,*groups])
    if not name or JUNK.search(text):return None
    if SPORT.search(text) or '体育' in groups:return '体育'
    if BBC.search(name):return '国际精选'
    if not HAN.search(name) and not KANA.search(name):return None
    inferred=regionalized_group(name,'中文综合')
    if inferred in MAINLAND_REGION_GROUPS:return inferred
    if inferred in {'香港','澳门','台湾','新加坡','马来西亚','日本'}:return inferred
    if inferred=='卫视台':return '卫视台'
    preferred=next((g for g in groups if g in {'中文付费','娱乐','少儿','音乐','教育','财经'}),'')
    if preferred:return preferred
    if PAY.search(name):return '中文付费'
    if KIDS.search(name):return '少儿'
    if MUSIC.search(name):return '音乐'
    if EDU.search(name):return '教育'
    if FIN.search(name):return '财经'
    if ENT.search(name):return '娱乐'
    if KANA.search(name) and not HAN.search(name):return '日本'
    return '其他地方'

def score(r):
    h=int(r.get('height') or 0);b=float(r.get('bitrate_mbps') or 0);d=float(r.get('segment_mbps') or 0);s=float(r.get('startup_s') or 0)
    val=h*100+min(b,30)*250+min(d,150)*8-min(s,30)*30
    if str(r.get('codec') or '').casefold() in {'hevc','h265'}:val+=250
    return val

def existing_extinf(name,group,existing,row):
    old=existing.get(canon(name))
    if old:
        line=old['extinf']
        if 'group-title=' in line:line=re.sub(r'group-title="[^"]*"',f'group-title="{group}"',line)
        else:line=line.replace('#EXTINF:-1',f'#EXTINF:-1 group-title="{group}"',1)
        return line
    return f'#EXTINF:-1 group-title="{group}" hk-height="{int(row.get("height") or 0)}" hk-codec="{row.get("codec","")}",{name}'

def main():
    core=blocks(Path('tv-core.m3u'));existing_blocks=blocks(Path('tv.m3u'));existing={canon(x['name']):x for x in existing_blocks}
    core_keys={canon(x['name']) for x in core};core_urls={x['url'] for x in core}
    by={}
    for raw in Path('harvest/candidates.jsonl').read_text(encoding='utf-8').splitlines():
        if not raw.strip():continue
        try:r=json.loads(raw)
        except:continue
        u=str(r.get('url') or '').strip();n=str(r.get('name') or '').strip();g=str(r.get('group') or '').strip();e=r.get('hk_verified') or {}
        if not u or not n or not isinstance(e,dict):continue
        x=by.setdefault(u,{'url':u,'names':[],'groups':[],'ev':e})
        if n not in x['names']:x['names'].append(n)
        if g and g not in x['groups']:x['groups'].append(g)
    best={};reject=Counter()
    for x in by.values():
        name=max(x['names'],key=lambda n:(1 if HAN.search(n) else 0,-len(n)))
        ev=x['ev'];h=int(ev.get('height') or 0)
        if h<720:reject['below720']+=1;continue
        if vodish(name,x['url']):reject['vod_or_loop']+=1;continue
        group=classify(name,x['groups'])
        if not group:reject['not_chinese_sports_bbc_or_junk']+=1;continue
        key=canon(name)
        if not key or key in core_keys or key=='cctv8k' or x['url'] in core_urls:continue
        row={'name':name,'group':group,'url':x['url'],**ev};row['rank']=score(row)
        if key not in best or row['rank']>best[key]['rank']:best[key]=row

    hd=sorted([r for r in best.values() if int(r.get('height') or 0)>=1080],key=lambda r:r['rank'],reverse=True)
    fallback=sorted([r for r in best.values() if 720<=int(r.get('height') or 0)<1080],key=lambda r:(1 if HAN.search(r['name']) else 0,1 if r['group']!='其他地方' else 0,r['rank']),reverse=True)
    chosen=[];used_keys=set(core_keys);used_urls=set(core_urls);sports=0
    def take(row):
        nonlocal sports
        key=canon(row['name'])
        if key in used_keys or row['url'] in used_urls:return False
        if row['group']=='体育' and sports>=SPORT_CAP:return False
        used_keys.add(key);used_urls.add(row['url']);chosen.append(row)
        if row['group']=='体育':sports+=1
        return True
    for r in hd:take(r)
    for r in fallback:
        if len(core)+len(chosen)>=TARGET:break
        take(r)
    if len(core)+len(chosen)<TARGET:
        # Last resort: permit additional verified sports before breaking the 400-channel floor.
        for r in hd+fallback:
            if len(core)+len(chosen)>=TARGET:break
            key=canon(r['name'])
            if key in used_keys or r['url'] in used_urls:continue
            used_keys.add(key);used_urls.add(r['url']);chosen.append(r)
            if r['group']=='体育':sports+=1
    chosen=chosen[:TARGET-len(core)]
    total=len(core)+len(chosen)
    if total<TARGET:raise SystemExit(f'clean verified inventory only produced {total}, need {TARGET}')

    order={g:i for i,g in enumerate(OUTPUT_GROUP_ORDER)}
    chosen.sort(key=lambda r:(order.get(r['group'],999),r['name']))
    out=['#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"',f'# HK verified balanced lineup: Chinese-first, HD-first, channels={total}',f'# generated_utc={time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}','']
    for b in core:out += [b['extinf'],b['url']]
    for r in chosen:out += [existing_extinf(r['name'],r['group'],existing,r),r['url']]
    text='\n'.join(out)+'\n'
    Path('tv.m3u').write_text(text,encoding='utf-8');Path('tv-all.m3u').write_text(text,encoding='utf-8')

    groups=Counter(b['group'] or '卫视台' for b in core);heights=Counter();noncore720=[]
    for r in chosen:
        groups[r['group']]+=1;h=int(r.get('height') or 0);heights['2160+' if h>=2160 else '1080' if h>=1080 else '720']+=1
        if h<1080:noncore720.append(r['name'])
    manifest={'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'stage':'hong_kong_verified_balanced_curated_v2','channels':total,'core_preserved':len(core),'sports_channels':groups.get('体育',0),'noncore_quality':dict(heights),'noncore_720_count':len(noncore720),'noncore_720_names':noncore720,'group_counts':dict(sorted(groups.items())),'rejected':dict(reject),'policy':{'chinese_first':True,'hd_first':True,'target_channels':TARGET,'sports_soft_cap':SPORT_CAP,'obvious_vod_and_loop_hosts_rejected':True,'generic_english_only_sports_or_bbc':True,'core_preserved_exactly':True}}
    Path('harvest/curated-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(f'CURATED_V2 channels={total} core={len(core)} sports={groups.get("体育",0)} noncore1080plus={heights.get("1080",0)+heights.get("2160+",0)} noncore720={len(noncore720)}')
    print('GROUPS='+json.dumps(dict(sorted(groups.items())),ensure_ascii=False))

if __name__=='__main__':main()
