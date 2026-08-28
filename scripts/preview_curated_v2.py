#!/usr/bin/env python3
from __future__ import annotations

import json,re
from collections import Counter,defaultdict
from pathlib import Path
from urllib.parse import urlparse
from channel_regions import MAINLAND_REGION_GROUPS, regionalized_group

HAN_RE=re.compile(r'[\u3400-\u9fff]')
KANA_RE=re.compile(r'[\u3040-\u30ff]')
SPORT_RE=re.compile(r'(?:体育|體育|足球|篮球|籃球|网球|網球|高尔夫|赛车|搏击|sport|espn|bein|eurosport|dazn|nba|nfl|mlb|nhl|ufc|formula\s*1|\bf1\b|motorsport|racing|golf|tennis|football|soccer|sky\s*sports|premier\s*sports|astro\s*supersport|纬来体育|緯來體育|博斯|五星体育)',re.I)
BBC_RE=re.compile(r'(?<![a-z])bbc(?:\s|[-_]|$)',re.I)
JUNK_RE=re.compile(r'(?:adult|xxx|porn|情色|成人|购物|購物|shopping|test\b|测试|測試|demo\b|radio\b|广播|廣播|电台|電台|weather\b|webcam|camera\b|traffic\b|免费订阅|請勿販賣|请勿贩卖|公告说明|公告說明)',re.I)
PAY_RE=re.compile(r'(?:CCTV.*(?:风云|風雲|第一剧场|第一劇場|怀旧|懷舊|文化精品|世界地理)|CHC|NewTV|SiTV|BesTV|百视通|百視通|动作电影|動作電影|家庭影院|家庭電影|影迷电影|影迷電影|武博世界)',re.I)
KIDS_RE=re.compile(r'(?:少儿|少兒|卡酷|优漫|優漫|嘉佳卡通|动漫|動漫|动画|動畫|金鹰卡通|金鷹卡通)',re.I)
MUSIC_RE=re.compile(r'(?:音乐|音樂|点歌|點歌|music)',re.I)
EDU_RE=re.compile(r'(?:教育|科教|课堂|課堂|大学|大學|考试|考試)',re.I)
FIN_RE=re.compile(r'(?:财经|財經|证券|證券|经济|經濟|金融)',re.I)
ENT_RE=re.compile(r'(?:电影|電影|影院|剧场|劇場|影视|影視|综艺|綜藝|戏曲|戲曲)',re.I)

BAD_HOST_BITS=('bdstatic.com','material.1989.click','goodiptv.club','metshop.top','ottiptv.cc')
BAD_PATH_BITS=('/huya/','/douyu/')


def canon(s):
    s=s.casefold().replace('＋','+')
    s=re.sub(r'\b(?:uhd|fhd|full\s*hd|hd|sd|hevc|h265|h264|avc)\b|(?:2160|1080|720|576|540|480)[pi]?|超高清|高清|标清|標清|蓝光|藍光','',s,flags=re.I)
    return re.sub(r'[\s\-_.·•()（）\[\]【】/\\]+','',s)[:160]

def core_keys():
    keys=set()
    for line in Path('tv-core.m3u').read_text(encoding='utf-8').splitlines():
        if line.startswith('#EXTINF:') and ',' in line:
            keys.add(canon(line.rsplit(',',1)[-1].strip()))
    return keys

def obvious_vod(name,url):
    low=url.lower()
    host=(urlparse(url).hostname or '').lower()
    if '.mp4' in low: return True
    if any(x in host for x in BAD_HOST_BITS): return True
    if any(x in low for x in BAD_PATH_BITS): return True
    if len(name) > 34 and not re.search(r'(电视|電視|频道|頻道|卫视|衛視|TV)',name,re.I): return True
    return False

def classify(name,groups):
    text=' '.join([name,*groups])
    if not name or JUNK_RE.search(text): return None
    if SPORT_RE.search(text) or '体育' in groups: return '体育'
    if BBC_RE.search(name): return '国际精选'
    if not HAN_RE.search(name) and not KANA_RE.search(name): return None
    preferred=next((g for g in groups if g),'中文综合')
    mapped=regionalized_group(name,preferred)
    if mapped in MAINLAND_REGION_GROUPS: return mapped
    if mapped in {'香港','澳门','台湾','新加坡','马来西亚','日本'}: return mapped
    if preferred in {'中文付费','娱乐','少儿','音乐','教育','财经'}: return preferred
    if PAY_RE.search(name): return '中文付费'
    if KIDS_RE.search(name): return '少儿'
    if MUSIC_RE.search(name): return '音乐'
    if EDU_RE.search(name): return '教育'
    if FIN_RE.search(name): return '财经'
    if ENT_RE.search(name): return '娱乐'
    if KANA_RE.search(name) and not HAN_RE.search(name): return '日本'
    return '其他地方'

def score(r):
    h=int(r.get('height') or 0); b=float(r.get('bitrate_mbps') or 0); d=float(r.get('segment_mbps') or 0); s=float(r.get('startup_s') or 0)
    return h*100+min(b,30)*250+min(d,150)*8-min(s,30)*30

def main():
    by_url={}
    for raw in Path('harvest/candidates.jsonl').read_text(encoding='utf-8').splitlines():
        if not raw.strip(): continue
        try:r=json.loads(raw)
        except:continue
        u=str(r.get('url') or '').strip(); n=str(r.get('name') or '').strip(); g=str(r.get('group') or '').strip(); ev=r.get('hk_verified') or {}
        if not u or not n or not isinstance(ev,dict): continue
        x=by_url.setdefault(u,{'url':u,'names':[],'groups':[],'ev':ev})
        if n not in x['names']:x['names'].append(n)
        if g and g not in x['groups']:x['groups'].append(g)
        old=x['ev']; new=ev
        if (int(new.get('height') or 0),float(new.get('bitrate_mbps') or 0),float(new.get('segment_mbps') or 0)) > (int(old.get('height') or 0),float(old.get('bitrate_mbps') or 0),float(old.get('segment_mbps') or 0)):
            x['ev']=new
    core=core_keys(); best={}; rejected=Counter(); elig_groups=Counter(); heights=Counter()
    for x in by_url.values():
        names=sorted(x['names'],key=lambda n:(1 if HAN_RE.search(n) else 0,-len(n)),reverse=True); name=names[0]
        ev=x['ev']; h=int(ev.get('height') or 0)
        if h < 720: rejected['below720']+=1; continue
        if obvious_vod(name,x['url']): rejected['obvious_vod_or_loop']+=1; continue
        group=classify(name,x['groups'])
        if not group: rejected['not_chinese_sports_bbc_or_junk']+=1; continue
        key=canon(name)
        if not key or key in core or key=='cctv8k': continue
        r={'name':name,'group':group,'url':x['url'],**ev}; r['rank']=score(r)
        if key not in best or r['rank']>best[key]['rank']:best[key]=r
    for r in best.values():
        elig_groups[r['group']]+=1; h=int(r.get('height') or 0)
        heights['2160+' if h>=2160 else '1080' if h>=1080 else '720']+=1
    ordered=sorted(best.values(),key=lambda r:(1 if r['group']!='其他地方' else 0,1 if int(r.get('height') or 0)>=1080 else 0,r['rank']),reverse=True)
    chosen=ordered[:447] # 53 core + 447 = 500
    ch_groups=Counter(r['group'] for r in chosen); ch_heights=Counter('2160+' if int(r.get('height') or 0)>=2160 else '1080' if int(r.get('height') or 0)>=1080 else '720' for r in chosen)
    print('POOL_UNIQUE_URLS',len(by_url))
    print('REJECTED',json.dumps(rejected,ensure_ascii=False,sort_keys=True))
    print('ELIGIBLE_IDENTITIES',len(best))
    print('ELIGIBLE_GROUPS',json.dumps(elig_groups,ensure_ascii=False,sort_keys=True))
    print('ELIGIBLE_HEIGHTS',json.dumps(heights,ensure_ascii=False,sort_keys=True))
    print('PREVIEW_TOTAL',53+len(chosen),'NONCORE',len(chosen))
    print('PREVIEW_GROUPS',json.dumps(ch_groups,ensure_ascii=False,sort_keys=True))
    print('PREVIEW_HEIGHTS',json.dumps(ch_heights,ensure_ascii=False,sort_keys=True))
    Path('audit/curated-v2-preview.json').write_text(json.dumps({'pool_unique_urls':len(by_url),'rejected':dict(rejected),'eligible_identities':len(best),'eligible_groups':dict(elig_groups),'eligible_heights':dict(heights),'preview_total':53+len(chosen),'preview_groups':dict(ch_groups),'preview_heights':dict(ch_heights),'chosen':[{'name':r['name'],'group':r['group'],'height':int(r.get('height') or 0),'url':r['url']} for r in chosen]},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':main()
