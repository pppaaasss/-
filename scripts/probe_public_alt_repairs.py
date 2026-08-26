#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, json, time
from dataclasses import asdict
from pathlib import Path
from probe_cctv5_hd import probe

CANDIDATES={
  'CCTV-5+':[
    ('play6','http://43.251.226.89:8080/play/6.m3u8'),
    ('dsdq','http://38.75.136.137:98/gslb/dsdqbv/cctv5p.m3u8?auth=test20251009'),
  ],
  'CCTV-6':[
    ('play7','http://43.251.226.89:8080/play/7.m3u8'),
    ('dsdq','http://38.75.136.137:98/gslb/dsdqbv/cctv6hd.m3u8?auth=test20251009'),
  ],
  'CCTV-8':[
    ('play9','http://43.251.226.89:8080/play/9.m3u8'),
    ('unicom124','http://124.165.251.82:85/tsfile/live/0008_1.m3u8?key=txiptv&playlive=1&authid=0'),
    ('dsdq','http://38.75.136.137:98/gslb/dsdqbv/cctv8hd.m3u8?auth=test20251009'),
  ],
  '吉林卫视':[
    ('play47','http://43.251.226.89:8080/play/47.m3u8'),
    ('dsdq','http://38.75.136.137:98/gslb/dsdqpub/jlwshd.m3u8?auth=testpub'),
    ('kkitv','http://api.kkitv.itv888.cn:8080/hls/b6dl8yuvu/index.m3u'),
  ],
  '陕西卫视':[
    ('play45','http://43.251.226.89:8080/play/45.m3u8'),
    ('dsdq','http://38.75.136.137:98/gslb/dsdqpub/sxwshd.m3u8?auth=testpub'),
  ],
}

def run(item):
  ch,label,url=item
  r=probe(label,url); d=asdict(r)
  md=min(r.download_mbps,r.second_download_mbps or r.download_mbps)
  d['min_download_mbps']=round(md,3); d['headroom']=round(md/r.stream_mbps,3) if r.stream_mbps else 0
  d['strict_pass']=bool(r.ok and 1080<=r.height<2160 and r.stream_mbps>=5 and md>=max(7,r.stream_mbps*1.2) and r.startup_s<=2)
  d['safe_hd_pass']=bool(r.ok and 1080<=r.height<2160 and r.stream_mbps>=2.5 and md>=max(5,r.stream_mbps*1.5) and r.startup_s<=2)
  return ch,d

def main():
  flat=[(ch,l,u) for ch,rows in CANDIDATES.items() for l,u in rows]
  with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex: raw=list(ex.map(run,flat))
  grouped={k:[] for k in CANDIDATES}
  for ch,d in raw: grouped[ch].append(d)
  Path('public-alt-repair-probe.txt').write_text(json.dumps({'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'results':grouped},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
  for ch,rows in grouped.items():
    print('===',ch,'===')
    for d in rows: print(f"{d['label']} ok={d['ok']} {d['width']}x{d['height']} fps={d['avg_fps']:.1f} stream={d['stream_mbps']:.2f} min_dl={d['min_download_mbps']:.2f} headroom={d['headroom']:.2f} start={d['startup_s']:.2f} strict={d['strict_pass']} safe={d['safe_hd_pass']} {d['url']}")
  return 0
if __name__=='__main__': raise SystemExit(main())
