#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, json, time
from dataclasses import asdict
from pathlib import Path
from probe_cctv5_hd import probe

CANDIDATES={
  'CCTV-5+':[
    ('jia-bj-unicom','http://114.243.99.254:8888/rtp/239.3.1.130:8004'),
    ('jia-gd-telecom-a','http://119.134.202.156:8889/rtp/239.77.0.87:5146'),
    ('jia-gd-telecom-b','http://119.134.202.156:8889/rtp/239.77.1.31:5146'),
    ('proxy','http://go.bkpcp.top/mg/cctv5p'),
  ],
  'CCTV-6':[
    ('jia-bj-unicom','http://114.243.99.254:8888/rtp/239.3.1.174:8001'),
    ('jia-gd-telecom-a','http://119.134.202.156:8889/rtp/239.77.0.35:5146'),
    ('jia-gd-telecom-b','http://119.134.202.156:8889/rtp/239.77.0.171:5146'),
    ('jia-gd-telecom-c','http://119.134.202.156:8889/rtp/239.77.1.9:5146'),
    ('public-hls-1.24','http://1.24.39.180:9003/hls/6/index.m3u8'),
  ],
  'CCTV-8':[
    ('jia-bj-unicom','http://114.243.99.254:8888/rtp/239.3.1.175:8001'),
    ('jia-gd-telecom-a','http://119.134.202.156:8889/rtp/239.77.0.37:5146'),
    ('jia-gd-telecom-b','http://119.134.202.156:8889/rtp/239.77.0.172:5146'),
    ('jia-gd-telecom-c','http://119.134.202.156:8889/rtp/239.77.1.10:5146'),
    ('public-hls-1.24','http://1.24.39.180:9003/hls/8/index.m3u8'),
  ],
  '吉林卫视':[
    ('jia-bj-unicom','http://114.243.99.254:8888/rtp/239.3.1.240:8172'),
    ('current-unicom','http://221.7.175.154:8445/tsfile/live/1014_1.m3u8?key=txiptv&playlive=1&authid=0'),
  ],
  '陕西卫视':[
    ('jia-bj-unicom','http://114.243.99.254:8888/rtp/239.3.1.41:8140'),
    ('public-107-sn','http://107.150.60.122/live/snwshd.m3u8'),
    ('public-63-sn','http://63.141.230.178:82/gslb/zbdq5.m3u8?id=snwshd'),
  ],
  '云南卫视':[
    ('jia-bj-unicom','http://114.243.99.254:8888/rtp/239.3.1.26:8108'),
    ('public-107','http://107.150.60.122/live/ynwshd.m3u8'),
    ('public-63','http://63.141.230.178:82/gslb/zbdq5.m3u8?id=ynwshd'),
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
  with concurrent.futures.ThreadPoolExecutor(max_workers=22) as ex: raw=list(ex.map(run,flat))
  grouped={k:[] for k in CANDIDATES}
  for ch,d in raw: grouped[ch].append(d)
  Path('final-repair-probe.txt').write_text(json.dumps({'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'results':grouped},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
  for ch,rows in grouped.items():
    print('===',ch,'===')
    for d in rows: print(f"{d['label']} ok={d['ok']} {d['width']}x{d['height']} fps={d['avg_fps']:.1f} stream={d['stream_mbps']:.2f} min_dl={d['min_download_mbps']:.2f} headroom={d['headroom']:.2f} start={d['startup_s']:.2f} strict={d['strict_pass']} safe={d['safe_hd_pass']} {d['url']}")
  return 0
if __name__=='__main__': raise SystemExit(main())
