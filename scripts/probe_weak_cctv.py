#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, json, time
from dataclasses import asdict
from pathlib import Path
from probe_cctv5_hd import probe

REPORT=Path('cctv-weak-upgrade-report.json')

# Fast shortlist from the IPTV repositories the user already supplied.
CANDIDATES={
 'CCTV-1':[
  ('iptvorg-69','http://69.30.245.50/live/cctv1.m3u8'),
  ('iptvorg-198','http://198.204.240.250:82/live/cctv1.m3u8'),
  ('xisohi-bfgd','http://120.76.248.139/live/bfgd/4200000489.m3u8'),
  ('xisohi-shandong-ts','http://58.57.40.22:9901/tsfile/live/0001_1.m3u8'),
 ],
 'CCTV-2':[
  ('xisohi-bfgd','http://120.76.248.139/live/bfgd/4200000061.m3u8'),
  ('xisohi-shandong-ts','http://58.57.40.22:9901/tsfile/live/1001_1.m3u8'),
  ('xisohi-hls-183','http://183.11.239.36:808/hls/20/index.m3u8'),
  ('iptvorg-74','http://74.91.26.218:82/live/cctv2hd.m3u8'),
 ],
 'CCTV-11':[
  ('xisohi-ts-61','http://61.136.172.236:9901/tsfile/live/0011_1.m3u8'),
  ('xisohi-kankan','https://play.kankanlive.com/live/1698423476198918.m3u8'),
  ('xisohi-hls-183','http://183.11.239.36:808/hls/29/index.m3u8'),
  ('iptvorg-xykt','https://xykt-fix.github.io/play/a02b/index.m3u8'),
 ],
 'CCTV-12':[
  ('xisohi-ts-61','http://61.136.172.236:9901/tsfile/live/0012_1.m3u8'),
  ('xisohi-shandong-ts','http://58.57.40.22:9901/tsfile/live/1012_1.m3u8'),
  ('xisohi-kankan','https://play.kankanlive.com/live/1698423511884917.m3u8'),
  ('xisohi-hls-183','http://183.11.239.36:808/hls/30/index.m3u8'),
 ],
 'CCTV-14':[
  ('xisohi-ts-61','http://61.136.172.236:9901/tsfile/live/0014_1.m3u8'),
  ('xisohi-shandong-ts','http://58.57.40.22:9901/tsfile/live/1014_1.m3u8'),
  ('xisohi-kankan','https://play.kankanlive.com/live/1698423575756915.m3u8'),
  ('xisohi-hebtv','https://event.pull.hebtv.com/jishi/cp2.m3u8'),
 ],
 'CCTV-17':[
  ('xisohi-shandong-ts','http://58.57.40.22:9901/tsfile/live/0019_1.m3u8'),
  ('xisohi-kankan','https://play.kankanlive.com/live/1698423272597921.m3u8'),
  ('xisohi-hls-183','http://183.11.239.36:808/hls/35/index.m3u8'),
  ('xisohi-hmfs','http://hmfs.f3322.net:3388/hls/37/index.m3u8'),
 ],
 'CCTV-4K':[
  ('xisohi-livephp','http://101.35.240.114:88/live.php?id=CCTV4K'),
  ('jia-guangdong-telecom','http://119.134.202.156:8889/rtp/239.77.0.194:5146'),
  ('jia-guizhou-telecom','http://218.86.143.57:9527/rtp/238.255.2.139:5999'),
  ('iptvorg-198','http://198.204.240.250:82/live/cctv4k.m3u8'),
 ],
}

def evaluate(channel,r):
 min_dl=min(r.download_mbps,r.second_download_mbps or r.download_mbps)
 headroom=min_dl/r.stream_mbps if r.stream_mbps else 0.0
 if channel=='CCTV-4K':
  passed=bool(r.ok and r.width>=3840 and r.height>=2160 and r.stream_mbps>=8.0 and min_dl>=max(15.0,r.stream_mbps*1.30) and r.startup_s<=3.0)
 else:
  passed=bool(r.ok and r.width>=1920 and r.height>=1080 and r.stream_mbps>=2.8 and min_dl>=max(6.0,r.stream_mbps*1.35) and r.startup_s<=2.5)
 return passed,min_dl,headroom

def one(item):
 ch,label,url=item
 r=probe(f'{ch}:{label}',url)
 d=asdict(r)
 passed,min_dl,headroom=evaluate(ch,r)
 d.update(channel=ch,candidate=label,passed=passed,min_download_mbps=round(min_dl,3),headroom=round(headroom,3))
 return d

def main():
 flat=[(ch,label,url) for ch,items in CANDIDATES.items() for label,url in items]
 with concurrent.futures.ThreadPoolExecutor(max_workers=28) as ex:
  rows=list(ex.map(one,flat))
 best={}; grouped={}
 for ch in CANDIDATES:
  g=[x for x in rows if x['channel']==ch]
  g.sort(key=lambda x:(x['passed'],x['height'],x['width'],x['stream_mbps'],x['min_download_mbps'],-x['startup_s']),reverse=True)
  grouped[ch]=g; best[ch]=next((x for x in g if x['passed']),None)
 payload={'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'best_passing':best,'results':grouped}
 REPORT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 for ch in CANDIDATES:
  b=best[ch]
  print('BEST',ch, 'NONE' if not b else f"{b['candidate']} {b['width']}x{b['height']} {b['stream_mbps']:.2f}Mbps dl={b['min_download_mbps']:.2f}")
  for x in grouped[ch]:
   print(('PASS' if x['passed'] else 'OK' if x['ok'] else 'FAIL'),ch,x['candidate'],f"{x['width']}x{x['height']}",f"stream={x['stream_mbps']:.2f}",f"dl={x['min_download_mbps']:.2f}")
 return 0
if __name__=='__main__': raise SystemExit(main())
