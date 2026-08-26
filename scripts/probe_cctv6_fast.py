#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, json, time
from dataclasses import asdict
from pathlib import Path
from probe_cctv5_hd import probe

REPORT=Path('cctv6-fast-probe-report.txt')
CANDIDATES=[
 ('fresh-153-ts','http://153.0.171.163:9901/tsfile/live/0006_1.m3u8?key=txiptv&playlive=1&authid=0'),
 ('fresh-192-public','http://192.151.150.154/live/cctv6hd.m3u8'),
 ('goodiptv-8m','https://live.goodiptv.club/api/bestv.php?id=cctv6hd8m/8000000'),
 ('henan-unicom','http://123.6.9.146/live/jz-cctv-6/live.m3u8'),
 ('gd-telecom-ts','http://183.63.15.42:9901/tsfile/live/0006_1.m3u8'),
 ('hn-telecom-ts','http://222.240.82.92:9901/tsfile/live/0006_1.m3u8'),
 ('cq-cable-cdn','http://baidu.live.cqccn.com/__cl/cg:live/__c/cctv6HD/__op/default/__f//index.m3u8'),
 ('public-hls-1.24','http://1.24.39.180:9003/hls/6/index.m3u8'),
 ('bestv-direct-180-8m','http://180.97.247.27:8088/liveplay-kk.rtxapp.com/live/program/live/cctv6hd8m/8000000/mnf.m3u8'),
 ('bestv-live-v1-8m','https://live.v1.mk/api/bestv.php?id=cctv6hd8m/8000000'),
 ('bestv-metshop-8m','https://live.metshop.top/bestv.php?id=cctv6hd8m/8000000'),
 ('public-119-8m','http://119.39.128.18:88/hls/6/index.m3u8'),
 ('bestv-old-proxy-8m','http://moss.hk3.345888.xyz.cdn.cloudflare.net/moss/bestv.php?id=cctv6hd8m/8000000'),
 ('public-198','http://198.204.228.26/live/cctv6hd.m3u8'),
 ('public-107','http://107.150.60.122/live/cctv6hd.m3u8'),
 ('public-74','http://74.91.26.218:82/live/cctv6hd.m3u8'),
 ('public-207','http://207.56.13.146:81/cdnlive/cctv6.m3u8'),
]

def one(item):
 label,url=item
 r=probe(label,url)
 d=asdict(r)
 min_dl=min(r.download_mbps,r.second_download_mbps or r.download_mbps)
 headroom=min_dl/r.stream_mbps if r.stream_mbps else 0
 passed=bool(r.ok and 1080<=r.height<2160 and r.stream_mbps>=6.0 and min_dl>=max(9.0,r.stream_mbps*1.30) and r.startup_s<=2.0)
 d.update(min_download_mbps=round(min_dl,3),headroom=round(headroom,3),passed=passed)
 return d

def main():
 with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
  rows=list(ex.map(one,CANDIDATES))
 rows.sort(key=lambda x:(x['passed'],x['height'],x['stream_mbps'],x['min_download_mbps'],-x['startup_s']),reverse=True)
 payload={'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'policy':{'min_height':1080,'min_stream_mbps':6.0,'min_download_mbps':9.0,'min_headroom':1.30,'max_startup_s':2.0},'best_passing':next((x for x in rows if x['passed']),None),'results':rows}
 REPORT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 for x in rows:
  state='PASS' if x['passed'] else ('OK' if x['ok'] else 'FAIL')
  print(f"{state:4} {x['label']:22} {x['width']}x{x['height']} stream={x['stream_mbps']:.2f}Mbps min_dl={x['min_download_mbps']:.2f}Mbps headroom={x['headroom']:.2f} start={x['startup_s']:.2f}s")
 return 0
if __name__=='__main__': raise SystemExit(main())
