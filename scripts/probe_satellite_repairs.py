#!/usr/bin/env python3
"""Probe replacement candidates for failed/wrong satellite TV routes only.

No playlist is modified.  Results are written to satellite-repair-probe.txt.
China Mobile PLTV/GMCC candidates are intentionally excluded.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import asdict
from pathlib import Path

from probe_cctv5_hd import probe

REPORT = Path("satellite-repair-probe.txt")
WORKERS = 18

CANDIDATES = {
    "湖南卫视": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=hunanwshd8m/8000000"),
        ("public-74", "http://74.91.26.218:82/live/hnwshd.m3u8"),
        ("public-192", "http://192.151.150.154/live/hnwshd.m3u8"),
        ("public-107", "http://107.150.60.122/live/hnwshd.m3u8"),
        ("public-198", "http://198.204.228.26/live/hnwshd.m3u8"),
    ],
    "黑龙江卫视": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=hljwshd8m/8000000"),
        ("public-198", "http://198.204.228.26/live/hljwshd.m3u8"),
        ("public-107", "http://107.150.60.122/live/hljwshd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/hljwshd.m3u8"),
        ("public-192", "http://192.151.150.154/live/hljwshd.m3u8"),
    ],
    "吉林卫视": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=jlwshd8m/8000000"),
        ("public-198", "http://198.204.228.26/live/jlwshd.m3u8"),
        ("public-107", "http://107.150.60.122/live/jlwshd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/jlwshd.m3u8"),
        ("public-192", "http://192.151.150.154/live/jlwshd.m3u8"),
    ],
    "海南卫视": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=hainanwshd8m/8000000"),
        ("public-198-ly", "http://198.204.228.26/live/lywshd.m3u8"),
        ("public-107-ly", "http://107.150.60.122/live/lywshd.m3u8"),
        ("public-74-ly", "http://74.91.26.218:82/live/lywshd.m3u8"),
        ("public-192-ly", "http://192.151.150.154/live/lywshd.m3u8"),
        ("public-api", "http://8.138.7.223/tv/xxqg1.php?id=hainanws"),
    ],
    "陕西卫视": [
        ("public-198", "http://198.204.228.26/live/shxwshd.m3u8"),
        ("public-107", "http://107.150.60.122/live/shxwshd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/shxwshd.m3u8"),
        ("public-192", "http://192.151.150.154/live/shxwshd.m3u8"),
        ("public-api", "http://8.138.7.223/tv/xxqg1.php?id=shanxiws"),
    ],
    "广西卫视": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=gxwshd8m/8000000"),
        ("public-198", "http://198.204.228.26/live/gxwshd.m3u8"),
        ("public-107", "http://107.150.60.122/live/gxwshd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/gxwshd.m3u8"),
        ("current-official", "https://hlscdn.liangtv.cn/live/de0f97348eb84f62aa6b7d8cf0430770/dd505d87880c478f901f38560ca4d4e6.m3u8"),
    ],
    "CCTV-5+": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=cctv5phd8m/8000000"),
        ("public-107", "http://107.150.60.122/live/cctv5p.m3u8"),
        ("public-198", "http://198.204.228.26/live/cctv5p.m3u8"),
    ],
    "CCTV-6": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=cctv6hd8m/8000000"),
        ("public-74", "http://74.91.26.218:82/live/cctv6hd.m3u8"),
        ("public-107", "http://107.150.60.122/live/cctv6hd.m3u8"),
    ],
    "CCTV-8": [
        ("jdshipin-8m", "http://nas.jdshipin.com:8801/bst.php?id=cctv8hd8m/8000000"),
        ("public-198", "http://198.204.228.26/live/cctv8hd.m3u8"),
        ("public-107", "http://107.150.60.122/live/cctv8hd.m3u8"),
        ("public-74", "http://74.91.26.218:82/live/cctv8hd.m3u8"),
    ],
}


def rank(row: dict) -> tuple:
    min_dl = min(row["download_mbps"], row["second_download_mbps"] or row["download_mbps"])
    stream = row["stream_mbps"]
    headroom = min_dl / stream if stream else 0.0
    quality = 2 if row["height"] >= 1080 else 1 if row["height"] >= 720 else 0
    return (row["ok"], quality, min(stream, 20), min(headroom, 20), min_dl, -row["startup_s"])


def main() -> int:
    flat = [(channel, label, url) for channel, rows in CANDIDATES.items() for label, url in rows]

    def run(item):
        channel, label, url = item
        result = probe(label, url)
        row = asdict(result)
        min_dl = min(result.download_mbps, result.second_download_mbps or result.download_mbps)
        row["min_download_mbps"] = round(min_dl, 3)
        row["headroom"] = round(min_dl / result.stream_mbps, 3) if result.stream_mbps else 0.0
        row["strict_pass"] = bool(
            result.ok and result.height >= 1080 and result.height < 2160
            and result.stream_mbps >= 5.0
            and min_dl >= max(7.0, result.stream_mbps * 1.2)
            and result.startup_s <= 2.0
        )
        row["safe_hd_pass"] = bool(
            result.ok and result.height >= 1080 and result.height < 2160
            and result.stream_mbps >= 2.5
            and min_dl >= max(5.0, result.stream_mbps * 1.5)
            and result.startup_s <= 2.0
        )
        return channel, row

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(run, flat))

    grouped = {name: [] for name in CANDIDATES}
    for channel, row in results:
        grouped[channel].append(row)
    for rows in grouped.values():
        rows.sort(key=rank, reverse=True)

    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": grouped,
    }
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for channel, rows in grouped.items():
        print(f"=== {channel} ===")
        for row in rows:
            state = "STRICT" if row["strict_pass"] else "SAFEHD" if row["safe_hd_pass"] else (row["mode"] if row["ok"] else "FAIL")
            print(
                f"{state:8} {row['label']:18} {row['width']}x{row['height']} "
                f"stream={row['stream_mbps']:.2f}Mbps min_dl={row['min_download_mbps']:.2f}Mbps "
                f"headroom={row['headroom']:.2f} start={row['startup_s']:.2f}s {row['url']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
