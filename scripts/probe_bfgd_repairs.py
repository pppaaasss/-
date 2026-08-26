#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, json, time
from dataclasses import asdict
from pathlib import Path
from probe_cctv5_hd import probe

CANDIDATES = {
    "CCTV-5+": "http://120.76.248.139/live/bfgd/4200000246.m3u8",
    "CCTV-6": "http://120.76.248.139/live/bfgd/4200000065.m3u8",
    "CCTV-8": "http://120.76.248.139/live/bfgd/4200000066.m3u8",
    "吉林卫视": "http://120.76.248.139/live/bfgd/4200000097.m3u8",
    "陕西卫视": "http://120.76.248.139/live/bfgd/4200000512.m3u8",
}

def one(item):
    name, url = item
    r = probe(name, url)
    d = asdict(r)
    md = min(r.download_mbps, r.second_download_mbps or r.download_mbps)
    d["min_download_mbps"] = round(md,3)
    d["headroom"] = round(md/r.stream_mbps,3) if r.stream_mbps else 0
    d["strict_pass"] = bool(r.ok and 1080 <= r.height < 2160 and r.stream_mbps >= 5 and md >= max(7,r.stream_mbps*1.2) and r.startup_s <= 2)
    d["safe_hd_pass"] = bool(r.ok and 1080 <= r.height < 2160 and r.stream_mbps >= 2.5 and md >= max(5,r.stream_mbps*1.5) and r.startup_s <= 2)
    return name,d

def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        rows=dict(ex.map(one,CANDIDATES.items()))
    Path("bfgd-repair-probe.txt").write_text(json.dumps({"generated_utc":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"results":rows},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    for n,r in rows.items():
        print(f"{n}: ok={r['ok']} {r['width']}x{r['height']} fps={r['avg_fps']:.1f} stream={r['stream_mbps']:.2f} min_dl={r['min_download_mbps']:.2f} headroom={r['headroom']:.2f} start={r['startup_s']:.2f} strict={r['strict_pass']} safe={r['safe_hd_pass']} {r['url']}")
    return 0
if __name__=='__main__': raise SystemExit(main())
