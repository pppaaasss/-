#!/usr/bin/env python3
"""Probe the user's priority TV channels before any playlist change.

This script never edits published playlists. It reuses the repository's real
ffprobe + HLS segment measurements and writes priority-probe-report.txt.
Candidates known to be China Mobile PLTV/GMCC routes are intentionally omitted.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import asdict
from pathlib import Path

from probe_cctv5_hd import probe

REPORT = Path("priority-probe-report.txt")
WORKERS = 16

CANDIDATES = {
    "CCTV-5": [
        ("aliyun-public-10m", "http://120.76.248.139/live/bfgd/4200000064.m3u8"),
        ("public-hls-1.24", "http://1.24.39.180:9003/hls/5/index.m3u8"),
        ("public-hls-113", "http://113.57.140.161:10081/newlive/live/hls/5/live.m3u8"),
        ("unicom-gateway", "http://221.7.175.154:8445/tsfile/live/1018_1.m3u8?key=txiptv&playlive=1&authid=0"),
        ("public-207", "http://207.56.13.146:81/cdnlive/cctv5.m3u8"),
        ("public-107", "http://107.150.60.122/live/cctv5hd.m3u8"),
    ],
    "CCTV-5+": [
        ("bestv-8m", "http://180.97.247.27:8088/liveplay-kk.rtxapp.com/live/program/live/cctv5phd8m/8000000/mnf.m3u8"),
        ("public-69", "http://69.30.246.194/live/cctv5p.m3u8"),
        ("public-198", "http://198.204.228.26/live/cctv5p.m3u8"),
        ("public-107", "http://107.150.60.122/live/cctv5p.m3u8"),
        ("public-63", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv5p"),
    ],
    "湖南卫视": [
        ("mgtv-high", "http://hlsal-ldvt.qing.mgtv.com/nn_live/nn_x64/aWQ9SE5XU1pHU1Qmcz0yNDAwJmQ9OTkmaHNpemU9MzIwMDAwMDAw/n_index.m3u8"),
        ("unicom-1080", "http://221.7.175.154:8445/tsfile/live/0128_1.m3u8?key=txiptv&playlive=1&authid=0"),
        ("public-198", "http://198.204.228.26/live/hnwshd.m3u8"),
        ("public-107", "http://107.150.60.122/live/hnwshd.m3u8"),
        ("public-63", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=hnwshd"),
        ("public-74", "http://74.91.26.218:82/live/hnwshd.m3u8"),
        ("public-192", "http://192.151.150.154/live/hnwshd.m3u8"),
        ("cbn-4m", "http://113.207.84.196/session/d8b2230e-9333-11ee-a43e-525400dfb345$h1.0$live.cbncdn.cn/pj9p9p/__cl/cg:live/__c/hunanHD/__op/default/__f/5/v4M/index.m3u8"),
    ],
    "CCTV-6": [
        ("public-119-8m", "http://119.39.128.18:88/hls/6/index.m3u8"),
        ("bestv-proxy-8m", "http://moss.hk3.345888.xyz.cdn.cloudflare.net/moss/bestv.php?id=cctv6hd8m/8000000"),
        ("public-198", "http://198.204.228.26/live/cctv6hd.m3u8"),
        ("public-107", "http://107.150.60.122/live/cctv6hd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/cctv6hd.m3u8"),
        ("public-207", "http://207.56.13.146:81/cdnlive/cctv6.m3u8"),
        ("current-69", "http://69.30.245.50/live/cctv6.m3u8"),
    ],
    "CCTV-8": [
        ("public-119-8m", "http://119.39.128.18:88/hls/8/index.m3u8"),
        ("bestv-proxy-8m", "http://moss.hk3.345888.xyz.cdn.cloudflare.net/moss/bestv.php?id=cctv8hd8m/8000000"),
        ("public-198", "http://198.204.228.26/live/cctv8hd.m3u8"),
        ("public-107", "http://107.150.60.122/live/cctv8hd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/cctv8hd.m3u8"),
        ("public-207", "http://207.56.13.146:81/cdnlive/cctv8.m3u8"),
    ],
}


def metrics(result):
    min_dl = min(result.download_mbps, result.second_download_mbps or result.download_mbps)
    headroom = min_dl / result.stream_mbps if result.stream_mbps else 0.0
    passed = bool(
        result.ok
        and 1080 <= result.height < 2160
        and result.stream_mbps >= 5.0
        and min_dl >= max(7.0, result.stream_mbps * 1.20)
        and result.startup_s <= 2.0
    )
    return min_dl, headroom, passed


def main() -> int:
    flat = [(channel, label, url) for channel, rows in CANDIDATES.items() for label, url in rows]

    def run(item):
        channel, label, url = item
        return channel, probe(label, url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        raw = list(executor.map(run, flat))

    grouped = {name: [] for name in CANDIDATES}
    for channel, result in raw:
        min_dl, headroom, passed = metrics(result)
        grouped[channel].append({
            **asdict(result),
            "min_download_mbps": round(min_dl, 3),
            "headroom": round(headroom, 3),
            "passed": passed,
        })

    for channel in grouped:
        grouped[channel].sort(
            key=lambda row: (
                row["passed"], row["height"], row["stream_mbps"],
                row["min_download_mbps"], -row["startup_s"]
            ),
            reverse=True,
        )

    best = {channel: next((row for row in rows if row["passed"]), None) for channel, rows in grouped.items()}
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "min_height": 1080,
            "reject_2160_plus_for_normal_channels": True,
            "min_stream_mbps": 5.0,
            "min_download_mbps": 7.0,
            "min_headroom": 1.20,
            "max_startup_s": 2.0,
            "china_mobile_routes_excluded": True,
        },
        "best_passing": best,
        "results": grouped,
    }
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for channel, rows in grouped.items():
        print(f"=== {channel} ===")
        for row in rows:
            state = "PASS" if row["passed"] else (row["mode"] if row["ok"] else "FAIL")
            print(
                f"{state:8} {row['label']:18} {row['width']}x{row['height']} "
                f"stream={row['stream_mbps']:.2f}Mbps min_dl={row['min_download_mbps']:.2f}Mbps "
                f"headroom={row['headroom']:.2f} start={row['startup_s']:.2f}s {row['url']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
