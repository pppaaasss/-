#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, json, time
from dataclasses import asdict
from pathlib import Path
from probe_cctv5_hd import probe

CANDIDATES={
  'CCTV-5+':[
    ('jilin-unicom','http://222.169.85.8:9901/tsfile/live/0116_1.m3u8'),
    ('proxy','http://go.bkpcp.top/mg/cctv5p'),
  ],
  'CCTV-6':[
    ('unicom-153','http://153.0.171.163:9901/tsfile/live/0006_1.m3u8?key=txiptv&playlive=1&authid=0'),
    ('public-204','http://204.12.221.218:8181/3m1080p/cctv6.m3u8'),
    ('public-182','http://182.140.125.47:808/hls/6/index.m3u8'),
  ],
  'CCTV-8':[
    ('unicom-153','http://153.0.171.163:9901/tsfile/live/1007_1.m3u8?key=txiptv&playlive=1&authid=0'),
    ('unicom-221','http://221.7.175.154:8445/tsfile/live/1006_1.m3u8?key=txiptv&playlive=1&authid=0'),
    ('public-69','http://69.30.245.50/live/cctv8.m3u8'),
  ],
  '吉林卫视':[
    ('current-unicom','http://221.7.175.154:8445/tsfile/live/1014_1.m3u8?key=txiptv&playlive=1&authid=0'),
  ],
  '陕西卫视':[
    ('public-107-sn','http://107.150.60.122/live/snwshd.m3u8'),
    ('public-63-sn','http://63.141.230.178:82/gslb/zbdq5.m3u8?id=snwshd'),
    ('unicom-gateway','http://113.25.252.226:9901/tsfile/live/1040_1.m3u8?key=txiptv&playlive=1&authid=0'),
    ('dynamic-gateway','http://genglei.8866.org:9901/tsfile/live/1030_1.m3u8?key=txiptv&playlive=1&authid=0'),
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
  with concurrent.futures.ThreadPoolExecutor(max_workers=14) as ex: raw=list(ex.map(run,flat))
  grouped={k:[] for k in CANDIDATES}
  for ch,d in raw: grouped[ch].append(d)
  Path('final-repair-probe.txt').write_text(json.dumps({'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'results':grouped},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
  for ch,rows in grouped.items():
    print('===',ch,'===')
    for d in rows: print(f"{d['label']} ok={d['ok']} {d['width']}x{d['height']} fps={d['avg_fps']:.1f} stream={d['stream_mbps']:.2f} min_dl={d['min_download_mbps']:.2f} headroom={d['headroom']:.2f} start={d['startup_s']:.2f} strict={d['strict_pass']} safe={d['safe_hd_pass']} {d['url']}")
  return 0
if __name__=='__main__': raise SystemExit(main())
