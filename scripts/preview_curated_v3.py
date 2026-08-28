#!/usr/bin/env python3
from __future__ import annotations
import json,re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from channel_regions import MAINLAND_REGION_GROUPS, regionalized_group

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

def cctv_id(s):
    x=s.casefold().replace('＋','+')
    if re.search(r'cctv\s*[-_ ]?8\s*k\b',x): return 'cctv8k'
    if re.search(r'cctv\s*[-_ ]?4\s*k\b',x): return 'cctv4k'
    if re.search(r'cctv\s*[-_ ]?5\s*(?:\+|plus)',x): return 'cctv5+'
    m=re.search(r'cctv\s*[-_ ]?(\d{1,2})(?!\s*k)',x)
    return f'cctv{int(m.group(1))}' if m else None

def canon(s):
    c=cctv_id(s)
    if c:return c
    x=s.casefold().replace('＋','+')
    x=re.sub(r'\b(?:uhd|fhd|full\s*hd|hd|sd|hevc|h265|h264|avc)\b|(?:2160|1080|720|576|540|480)[pi]?|超高清|高清|标清|標清|蓝光|藍光','',x,flags=re.I)
    return re.sub(r'[\s\-_.·•()（）\[\]【】/\\]+','',x)[:160]

def core_keys():
    out=set()
    for line in Path('tv-core.m3u').read_text(encoding='utf-8').splitlines():
        if line.startswith('#EXTINF:') and ',' in line:out.add(canon(line.rsplit(',',1)[-1].strip()))
    return out

def vodish(name,url):
    low=url.lower(); host=(urlparse(url).hostname or '').lower()
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
    return h*100+min(b,30)*250+min(d,150)*8-min(s,30)*30

def main():
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
    core=core_keys();best={};reject=Counter()
    for x in by.values():
        name=max(x['names'],key=lambda n:(1 if HAN.search(n) else 0,-len(n)))
        ev=x['ev'];h=int(ev.get('height') or 0)
        if h<1080:reject['below1080']+=1;continue
        if vodish(name,x['url']):reject['vod_or_loop']+=1;continue
        group=classify(name,x['groups'])
        if not group:reject['not_chinese_sports_bbc_or_junk']+=1;continue
        key=canon(name)
        if not key or key in core or key=='cctv8k':continue
        row={'name':name,'group':group,'url':x['url'],**ev};row['rank']=score(row)
        if key not in best or row['rank']>best[key]['rank']:best[key]=row
    groups=Counter(r['group'] for r in best.values())
    print('HD_ELIGIBLE',len(best),'TOTAL_WITH_CORE',53+len(best))
    print('HD_GROUPS',json.dumps(groups,ensure_ascii=False,sort_keys=True))
    print('REJECTED',json.dumps(reject,ensure_ascii=False,sort_keys=True))
    Path('audit/curated-v3-preview.json').write_text(json.dumps({'hd_eligible':len(best),'total_with_core':53+len(best),'groups':dict(groups),'rejected':dict(reject),'rows':sorted([{'name':r['name'],'group':r['group'],'height':int(r.get('height') or 0),'bitrate_mbps':float(r.get('bitrate_mbps') or 0),'url':r['url']} for r in best.values()],key=lambda x:(x['group'],x['name']))},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
