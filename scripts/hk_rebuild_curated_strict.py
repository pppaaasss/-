#!/usr/bin/env python3
from __future__ import annotations

import json,re,time
from collections import Counter
from pathlib import Path
import hk_rebuild_curated_from_verified_pool as base
from channel_regions import OUTPUT_GROUP_ORDER

TARGET=400
SPORT_CAP=40
SCENIC_RE=re.compile(r'(?:iPanda|自然保护区|自然保護區|景区直播|景區直播|风景直播|風景直播|监控|監控)',re.I)
CATEGORY_TAG_RE=re.compile(r'[「【\[(（]?\s*(?:体育|體育|少儿|少兒|音乐|音樂|教育|财经|財經|娱乐|娛樂)\s*[」】\])）]?',re.I)
PRIORITY_GROUPS=set(base.MAINLAND_REGION_GROUPS)|{'香港','澳门','台湾','新加坡','马来西亚','中文付费','娱乐','少儿','音乐','教育','财经'}


def meaningful_chinese(name:str)->bool:
    cleaned=CATEGORY_TAG_RE.sub('',name)
    return bool(base.HAN.search(cleaned))


def main():
    core=base.blocks(Path('tv-core.m3u'))
    existing_blocks=base.blocks(Path('tv.m3u'))
    existing={base.canon(x['name']):x for x in existing_blocks}
    core_keys={base.canon(x['name']) for x in core}; core_urls={x['url'] for x in core}

    by={}
    for raw in Path('harvest/candidates.jsonl').read_text(encoding='utf-8').splitlines():
        if not raw.strip():continue
        try:r=json.loads(raw)
        except Exception:continue
        u=str(r.get('url') or '').strip();n=str(r.get('name') or '').strip();g=str(r.get('group') or '').strip();e=r.get('hk_verified') or {}
        if not u or not n or not isinstance(e,dict):continue
        x=by.setdefault(u,{'url':u,'names':[],'groups':[],'ev':e})
        if n not in x['names']:x['names'].append(n)
        if g and g not in x['groups']:x['groups'].append(g)

    best={}; reject=Counter()
    for x in by.values():
        name=max(x['names'],key=lambda n:(1 if base.HAN.search(n) else 0,-len(n)))
        ev=x['ev'];h=int(ev.get('height') or 0)
        if h<720:reject['below720']+=1;continue
        if base.vodish(name,x['url']):reject['vod_or_loop']+=1;continue
        if SCENIC_RE.search(name):reject['scenic_or_camera']+=1;continue
        group=base.classify(name,x['groups'])
        if not group:reject['not_chinese_sports_bbc_or_junk']+=1;continue
        if group=='卫视台':reject['noncore_satellite_duplicate_class']+=1;continue
        if not meaningful_chinese(name) and group not in {'体育','国际精选'}:
            reject['english_disguised_by_category_tag']+=1;continue
        key=base.canon(name)
        if not key or key in core_keys or key=='cctv8k' or x['url'] in core_urls:continue
        row={'name':name,'group':group,'url':x['url'],**ev};row['rank']=base.score(row)
        if key not in best or row['rank']>best[key]['rank']:best[key]=row

    hd=sorted([r for r in best.values() if int(r.get('height') or 0)>=1080],key=lambda r:r['rank'],reverse=True)
    sd=sorted([r for r in best.values() if 720<=int(r.get('height') or 0)<1080],key=lambda r:(1 if r['group'] in PRIORITY_GROUPS else 0,1 if meaningful_chinese(r['name']) else 0,1 if r['group']!='其他地方' else 0,r['rank']),reverse=True)
    bbc=sorted([r for r in best.values() if r['group']=='国际精选'],key=lambda r:r['rank'],reverse=True)

    chosen=[];used_keys=set(core_keys);used_urls=set(core_urls);sports=0
    def take(r,ignore_sport_cap=False):
        nonlocal sports
        key=base.canon(r['name'])
        if key in used_keys or r['url'] in used_urls:return False
        if r['group']=='体育' and sports>=SPORT_CAP and not ignore_sport_cap:return False
        used_keys.add(key);used_urls.add(r['url']);chosen.append(r)
        if r['group']=='体育':sports+=1
        return True

    # Keep one BBC-class service when available; user explicitly allows BBC.
    if bbc:take(bbc[0])
    for r in hd:take(r)
    for r in sd:
        if len(core)+len(chosen)>=TARGET:break
        take(r)
    # Only if clean Chinese inventory cannot reach the requested lower bound,
    # permit extra verified sports rather than reintroducing VOD/junk.
    if len(core)+len(chosen)<TARGET:
        for r in hd+sd:
            if len(core)+len(chosen)>=TARGET:break
            take(r,ignore_sport_cap=True)
    chosen=chosen[:TARGET-len(core)]
    total=len(core)+len(chosen)
    if total<TARGET:raise SystemExit(f'clean inventory only produced {total}')

    order={g:i for i,g in enumerate(OUTPUT_GROUP_ORDER)}
    chosen.sort(key=lambda r:(order.get(r['group'],999),r['name']))
    out=['#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"',f'# HK verified strict lineup: Chinese-first, HD-first, channels={total}',f'# generated_utc={time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}','']
    for b in core:out += [b['extinf'],b['url']]
    for r in chosen:out += [base.existing_extinf(r['name'],r['group'],existing,r),r['url']]
    text='\n'.join(out)+'\n';Path('tv.m3u').write_text(text,encoding='utf-8');Path('tv-all.m3u').write_text(text,encoding='utf-8')

    groups=Counter(b['group'] or '卫视台' for b in core);heights=Counter();fallback=[]
    for r in chosen:
        groups[r['group']]+=1;h=int(r.get('height') or 0);heights['2160+' if h>=2160 else '1080' if h>=1080 else '720']+=1
        if h<1080:fallback.append(r['name'])
    manifest={'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'stage':'hong_kong_verified_strict_curated_v3','channels':total,'core_preserved':len(core),'sports_channels':groups.get('体育',0),'bbc_channels':groups.get('国际精选',0),'noncore_quality':dict(heights),'noncore_720_count':len(fallback),'noncore_720_names':fallback,'group_counts':dict(sorted(groups.items())),'rejected':dict(reject),'policy':{'chinese_first':True,'hd_first':True,'target_channels':TARGET,'sports_soft_cap':SPORT_CAP,'noncore_satellite_duplicates_rejected':True,'obvious_vod_loop_scenic_rejected':True,'generic_english_only_sports_or_bbc':True,'core_preserved_exactly':True}}
    Path('harvest/curated-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(f'STRICT_CURATED channels={total} core={len(core)} sports={groups.get("体育",0)} bbc={groups.get("国际精选",0)} hd={heights.get("1080",0)+heights.get("2160+",0)} fallback720={len(fallback)}')
    print('GROUPS='+json.dumps(dict(sorted(groups.items())),ensure_ascii=False))

if __name__=='__main__':main()
