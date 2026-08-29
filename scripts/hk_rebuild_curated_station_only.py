#!/usr/bin/env python3
from __future__ import annotations
import json,re,time
from collections import Counter
from pathlib import Path
import hk_rebuild_curated_from_verified_pool as base
from channel_regions import OUTPUT_GROUP_ORDER

MIN_CHANNELS=400
MAX_CHANNELS=500
SPORT_CAP=40
JAPAN_TARGET=20
SCENIC=re.compile(r'(?:iPanda|自然保护区|自然保護區|景区直播|景區直播|风景直播|風景直播|监控|監控)',re.I)
FOREIGN_TAG=re.compile(r'(?:CGTN.*(?:英语|英文|法语|法語|西语|西語|阿语|阿語|俄语|俄語)|「英文」|【英文】)',re.I)
HANGUL=re.compile(r'[\uac00-\ud7af]')
CATEGORY_TAG=re.compile(r'[「【\[(（]?\s*(?:体育|體育|少儿|少兒|音乐|音樂|教育|财经|財經|娱乐|娛樂|自然)\s*[」】\])）]?',re.I)
STATIONISH=re.compile(r'(?:电视|電視|电视台|電視台|频道|頻道|新闻|新聞|综合|綜合|公共|都市|生活|民生|农业|農業|农牧|農牧|交通|旅游|旅遊|纪实|紀實|法治|文旅|文体|文體|文化|经济|經濟|财经|財經|教育|科教|少儿|少兒|影视|影視|电影|電影|剧场|劇場|体育|體育|音乐|音樂|戏曲|戲曲|健康|卫视|衛視|地理|科技|宝库|寶庫|时尚|時尚|梨园|梨園|武术|武術|睛彩|美亚|美亞|CGTN|CNA|TV$|台$)',re.I)
EPISODIC=re.compile(r'(?:春晚|第一季|第二季|第三季|第四季|第五季|虎牙[-—]|斗鱼[-—]|斗魚[-—])',re.I)
PRIORITY=set(base.MAINLAND_REGION_GROUPS)|{'香港','澳门','台湾','新加坡','马来西亚','日本','中文付费','娱乐','少儿','音乐','教育','财经'}

def meaningful_han(name):
    return bool(base.HAN.search(CATEGORY_TAG.sub('',name)))

def meaningful_local_name(name,group):
    cleaned=CATEGORY_TAG.sub('',name)
    return bool(base.HAN.search(cleaned) or (group=='日本' and base.KANA.search(cleaned)))

def station_ok(name,group):
    if group in {'体育','国际精选','中文付费','娱乐','少儿','音乐','教育','财经'}:return True
    if group in set(base.MAINLAND_REGION_GROUPS)|{'香港','澳门','台湾','新加坡','马来西亚','日本'}:
        return not EPISODIC.search(name)
    if group=='其他地方':return bool(STATIONISH.search(name)) and not EPISODIC.search(name)
    return False

def main():
    core=base.blocks(Path('tv-core.m3u'));existing_blocks=base.blocks(Path('tv.m3u'));existing={base.canon(x['name']):x for x in existing_blocks}
    core_keys={base.canon(x['name']) for x in core};core_urls={x['url'] for x in core}
    by={}
    for raw in Path('harvest/candidates.jsonl').read_text(encoding='utf-8').splitlines():
        if not raw.strip():continue
        try:r=json.loads(raw)
        except Exception:continue
        u=str(r.get('url') or '').strip();n=str(r.get('name') or '').strip();g=str(r.get('group') or '').strip();ev=r.get('hk_verified') or {}
        if not u or not n or not isinstance(ev,dict):continue
        x=by.setdefault(u,{'url':u,'names':[],'groups':[],'ev':ev})
        if n not in x['names']:x['names'].append(n)
        if g and g not in x['groups']:x['groups'].append(g)
    best={};reject=Counter()
    for x in by.values():
        name=max(x['names'],key=lambda n:(1 if base.HAN.search(n) else 0,-len(n)))
        ev=x['ev'];h=int(ev.get('height') or 0)
        if h<720:reject['below720']+=1;continue
        if base.vodish(name,x['url']):reject['vod_or_loop']+=1;continue
        if SCENIC.search(name):reject['scenic_or_camera']+=1;continue
        if FOREIGN_TAG.search(name):reject['foreign_language_variant']+=1;continue
        if HANGUL.search(name):reject['korean_station']+=1;continue
        group=base.classify(name,x['groups'])
        if not group:reject['not_chinese_sports_bbc_or_junk']+=1;continue
        if group=='卫视台':reject['noncore_satellite_duplicate_class']+=1;continue
        if not meaningful_local_name(name,group) and group not in {'体育','国际精选'}:reject['english_disguised']+=1;continue
        if not station_ok(name,group):reject['not_station_like']+=1;continue
        key=base.canon(name)
        if not key or key in core_keys or key=='cctv8k' or x['url'] in core_urls:continue
        row={'name':name,'group':group,'url':x['url'],**ev};row['rank']=base.score(row)
        if key not in best or row['rank']>best[key]['rank']:best[key]=row
    hd=sorted([r for r in best.values() if int(r.get('height') or 0)>=1080],key=lambda r:r['rank'],reverse=True)
    fb=sorted([r for r in best.values() if 720<=int(r.get('height') or 0)<1080],key=lambda r:(1 if r['group'] in PRIORITY else 0,1 if meaningful_local_name(r['name'],r['group']) else 0,1 if r['group']!='其他地方' else 0,r['rank']),reverse=True)
    bbc=sorted([r for r in best.values() if r['group']=='国际精选'],key=lambda r:r['rank'],reverse=True)
    japan=sorted([r for r in hd+fb if r['group']=='日本'],key=lambda r:(1 if int(r.get('height') or 0)>=1080 else 0,r['rank']),reverse=True)
    chosen=[];used_keys=set(core_keys);used_urls=set(core_urls);sports=0
    def take(r,ignore=False):
        nonlocal sports
        k=base.canon(r['name'])
        if k in used_keys or r['url'] in used_urls:return False
        if r['group']=='体育' and sports>=SPORT_CAP and not ignore:return False
        if len(core)+len(chosen)>=MAX_CHANNELS:return False
        used_keys.add(k);used_urls.add(r['url']);chosen.append(r)
        if r['group']=='体育':sports+=1
        return True
    # Reserve up to 20 genuinely Hong Kong-verified Japanese stations before
    # the global HD ranking can crowd them out with mainland overflow.
    for r in japan:
        if sum(1 for x in chosen if x['group']=='日本')>=JAPAN_TARGET:break
        take(r)
    if bbc:take(bbc[0])
    # Quality-first range policy: keep every verified 1080p/2160p station up to
    # the 500-channel ceiling. Only use 720p fallbacks when needed to reach the
    # 400-channel floor, so the output is allowed to naturally land anywhere
    # from 400 to 500 instead of being forced to exactly 400.
    for r in hd:
        if len(core)+len(chosen)>=MAX_CHANNELS:break
        take(r)
    for r in fb:
        if len(core)+len(chosen)>=MIN_CHANNELS:break
        take(r)
    if len(core)+len(chosen)<MIN_CHANNELS:
        # Last resort: permit additional verified sports only to satisfy the
        # user's minimum lineup size. Never exceed the 500-channel ceiling.
        for r in hd+fb:
            if len(core)+len(chosen)>=MIN_CHANNELS:break
            take(r,ignore=True)
    total=len(core)+len(chosen)
    if total<MIN_CHANNELS:raise SystemExit(f'clean station inventory produced {total}, need at least {MIN_CHANNELS}')
    order={g:i for i,g in enumerate(OUTPUT_GROUP_ORDER)};chosen.sort(key=lambda r:(order.get(r['group'],999),r['name']))
    out=['#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"',f'# HK verified station-only lineup: Chinese-first, channels={total}',f'# generated_utc={time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}','']
    for b in core:out += [b['extinf'],b['url']]
    for r in chosen:out += [base.existing_extinf(r['name'],r['group'],existing,r),r['url']]
    text='\n'.join(out)+'\n';Path('tv.m3u').write_text(text,encoding='utf-8');Path('tv-all.m3u').write_text(text,encoding='utf-8')
    groups=Counter(b['group'] or '卫视台' for b in core);heights=Counter();fallback=[]
    for r in chosen:
        groups[r['group']]+=1;h=int(r.get('height') or 0);heights['2160+' if h>=2160 else '1080' if h>=1080 else '720']+=1
        if h<1080:fallback.append(r['name'])
    m={'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'stage':'hong_kong_verified_station_only_curated_v6','channels':total,'core_preserved':len(core),'sports_channels':groups.get('体育',0),'bbc_channels':groups.get('国际精选',0),'noncore_quality':dict(heights),'noncore_720_count':len(fallback),'noncore_720_names':fallback,'group_counts':dict(sorted(groups.items())),'rejected':dict(reject),'policy':{'station_like_only':True,'chinese_first':True,'hd_first':True,'min_channels':MIN_CHANNELS,'max_channels':MAX_CHANNELS,'quality_first_variable_size':True,'sports_soft_cap':SPORT_CAP,'japan_target':JAPAN_TARGET,'japan_kana_allowed':True,'noncore_satellite_duplicates_rejected':True,'vod_loop_scenic_event_series_rejected':True,'generic_english_only_sports_or_bbc':True,'core_preserved_exactly':True}}
    Path('harvest/curated-manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(f'STATION_ONLY channels={total} range={MIN_CHANNELS}-{MAX_CHANNELS} core={len(core)} sports={groups.get("体育",0)} bbc={groups.get("国际精选",0)} japan={groups.get("日本",0)} hd={heights.get("1080",0)+heights.get("2160+",0)} fallback720={len(fallback)}')
    print('GROUPS='+json.dumps(dict(sorted(groups.items())),ensure_ascii=False))

if __name__=='__main__':main()
