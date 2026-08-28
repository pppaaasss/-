#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from channel_regions import regionalized_group

PLAYLIST = Path('tv.m3u')
POOL = Path('harvest/candidates.jsonl')
OUT_JSON = Path('audit/curated-lineup-audit-2026-08-28.json')
OUT_TXT = Path('audit/curated-lineup-audit-2026-08-28.txt')

QUALITY_RE = re.compile(r'(?:\b(?:uhd|fhd|full\s*hd|hd|sd|hevc|h265|h264|avc)\b|(?:2160|1080|720|576|540|480)[pi]?|超高清|高清|标清|標清|蓝光|藍光|\b(?:25|50|60)fps\b)', re.I)
JUNK_RE = re.compile(r'(?:adult|xxx|porn|情色|成人|购物|購物|shopping|test\b|测试|測試|demo\b|radio\b|广播|廣播|电台|電台|weather\b|webcam|camera\b|traffic\b)', re.I)
ENGLISH_RE = re.compile(r'^[\x00-\x7f]+$')
SPORT_RE = re.compile(r'(?:体育|體育|足球|篮球|籃球|网球|網球|golf|tennis|sport|espn|bein|nba|nfl|mlb|nhl|ufc|racing|football|soccer|f1)', re.I)
BBC_RE = re.compile(r'(?<![a-z])bbc(?:\s|[-_]|$)', re.I)


def blocks(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()
    out=[]
    for i,line in enumerate(lines[:-1]):
        if not line.startswith('#EXTINF:') or ',' not in line:
            continue
        name=line.rsplit(',',1)[-1].strip()
        j=i+1
        while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith('#')):
            j += 1
        if j >= len(lines):
            continue
        url=lines[j].strip()
        if not url.startswith(('http://','https://')):
            continue
        gm=re.search(r'group-title="([^"]+)"',line)
        out.append({'name':name,'url':url,'group':gm.group(1) if gm else ''})
    return out


def canonical(raw: str):
    s=raw.casefold().replace('＋','+')
    s=QUALITY_RE.sub('',s)
    s=re.sub(r'[\s\-_.·•()（）\[\]【】/\\]+','',s)
    return s[:160]


def pool_evidence(path: Path):
    by_url={}
    if not path.exists():
        return by_url
    for raw in path.read_text(encoding='utf-8').splitlines():
        if not raw.strip():
            continue
        try: row=json.loads(raw)
        except Exception: continue
        url=str(row.get('url') or '').strip()
        ev=row.get('hk_verified') or {}
        if not url or not isinstance(ev,dict):
            continue
        old=by_url.get(url)
        score=(int(ev.get('height') or 0), float(ev.get('bitrate_mbps') or 0), float(ev.get('segment_mbps') or 0))
        if old is None or score > old['_score']:
            by_url[url]={**ev,'_score':score}
    return by_url


def main():
    entries=blocks(PLAYLIST)
    evmap=pool_evidence(POOL)
    name_counts=Counter(canonical(x['name']) for x in entries)
    url_counts=Counter(x['url'] for x in entries)
    groups=Counter(x['group'] for x in entries)

    duplicate_names=[x['name'] for x in entries if name_counts[canonical(x['name'])] > 1]
    duplicate_urls=[x['url'] for x in entries if url_counts[x['url']] > 1]
    evidence_missing=[]
    low_quality=[]
    quality=Counter()
    junk=[]
    suspicious_english=[]
    other_names=[]
    other_regroup=[]

    for x in entries:
        name=x['name']; url=x['url']; group=x['group']
        ev=evmap.get(url)
        if ev is None:
            evidence_missing.append(name)
        else:
            h=int(ev.get('height') or 0)
            br=float(ev.get('bitrate_mbps') or 0)
            seg=bool(ev.get('segment_ok'))
            codec=str(ev.get('codec') or '').casefold()
            if h >= 2160: quality['2160+'] += 1
            elif h >= 1080: quality['1080'] += 1
            elif h >= 720: quality['720'] += 1
            elif h > 0: quality['below720'] += 1
            else: quality['unknown_height'] += 1
            if h < 720 or not seg:
                low_quality.append({'name':name,'group':group,'height':h,'codec':codec,'bitrate_mbps':br,'segment_ok':seg,'url':url})
        if JUNK_RE.search(name): junk.append(name)
        if ENGLISH_RE.fullmatch(name) and not SPORT_RE.search(name) and not BBC_RE.search(name):
            suspicious_english.append(name)
        if group == '其他地方':
            other_names.append(name)
            mapped=regionalized_group(name,'中文综合')
            if mapped not in {'其他地方','中文综合',''}:
                other_regroup.append({'name':name,'suggested_group':mapped})

    payload={
        'channels':len(entries),
        'unique_channel_identities':len(set(canonical(x['name']) for x in entries)),
        'unique_urls':len(set(x['url'] for x in entries)),
        'group_counts':dict(groups),
        'quality_counts':dict(quality),
        'evidence_covered':len(entries)-len(evidence_missing),
        'evidence_missing_count':len(evidence_missing),
        'evidence_missing_names':evidence_missing,
        'duplicate_name_count':len(set(duplicate_names)),
        'duplicate_names':sorted(set(duplicate_names)),
        'duplicate_url_count':len(set(duplicate_urls)),
        'duplicate_urls':sorted(set(duplicate_urls)),
        'low_quality_count':len(low_quality),
        'low_quality':low_quality,
        'junk_name_count':len(junk),
        'junk_names':sorted(set(junk)),
        'suspicious_generic_english_count':len(suspicious_english),
        'suspicious_generic_english':sorted(set(suspicious_english)),
        'other_group_count':len(other_names),
        'other_regroupable_count':len(other_regroup),
        'other_regroupable':other_regroup,
        'other_sample':other_names[:160],
    }
    OUT_JSON.parent.mkdir(parents=True,exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    lines=[]
    lines.append(f"CHANNELS={payload['channels']} UNIQUE_IDENTITIES={payload['unique_channel_identities']} UNIQUE_URLS={payload['unique_urls']}")
    lines.append('GROUPS='+json.dumps(payload['group_counts'],ensure_ascii=False,sort_keys=True))
    lines.append('QUALITY='+json.dumps(payload['quality_counts'],ensure_ascii=False,sort_keys=True))
    lines.append(f"EVIDENCE_COVERED={payload['evidence_covered']} MISSING={payload['evidence_missing_count']}")
    lines.append(f"DUP_NAMES={payload['duplicate_name_count']} DUP_URLS={payload['duplicate_url_count']} LOW_QUALITY={payload['low_quality_count']}")
    lines.append(f"JUNK_NAMES={payload['junk_name_count']} GENERIC_ENGLISH={payload['suspicious_generic_english_count']}")
    lines.append(f"OTHER={payload['other_group_count']} REGROUPABLE={payload['other_regroupable_count']}")
    lines.append('\n== EVIDENCE MISSING ==')
    lines.extend(evidence_missing[:100] or ['NONE'])
    lines.append('\n== LOW QUALITY ==')
    lines.extend([f"{r['name']} | {r['height']}p {r['codec']} {r['bitrate_mbps']:.3f}M segment={r['segment_ok']} | {r['url']}" for r in low_quality[:100]] or ['NONE'])
    lines.append('\n== DUPLICATE NAMES ==')
    lines.extend(sorted(set(duplicate_names))[:100] or ['NONE'])
    lines.append('\n== GENERIC ENGLISH ==')
    lines.extend(sorted(set(suspicious_english))[:100] or ['NONE'])
    lines.append('\n== OTHER REGROUPABLE ==')
    lines.extend([f"{r['name']} -> {r['suggested_group']}" for r in other_regroup[:160]] or ['NONE'])
    lines.append('\n== OTHER SAMPLE ==')
    lines.extend(other_names[:160] or ['NONE'])
    OUT_TXT.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines[:7]))

if __name__=='__main__':
    main()
