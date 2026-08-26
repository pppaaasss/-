#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import asdict
from pathlib import Path

from probe_cctv5_hd import probe

REPORT = Path('cctv-weak-upgrade-report.json')

# Candidates are intentionally limited to repositories the user already supplied:
# xisohi/CHINA-IPTV, iptv-org/iptv, and jia070310/4K-IPTV-M3U.
CANDIDATES = {
    'CCTV-1': [
        ('iptvorg-69', 'http://69.30.245.50/live/cctv1.m3u8'),
        ('iptvorg-198', 'http://198.204.240.250:82/live/cctv1.m3u8'),
        ('xisohi-bfgd-a', 'http://120.76.248.139/live/bfgd/4200000489.m3u8'),
        ('xisohi-bfgd-b', 'http://120.76.248.139/live/bfgd/4200000484.m3u8'),
        ('xisohi-jilin-ts', 'http://222.169.85.8:9901/tsfile/live/0001_1.m3u8'),
        ('xisohi-shandong-ts', 'http://58.57.40.22:9901/tsfile/live/0001_1.m3u8'),
        ('xisohi-kankan-a', 'http://play.kankanlive.com/live/1698234869325962.m3u8'),
        ('jia-beijing-unicom', 'http://114.243.99.254:8888/rtp/239.3.1.129:8008'),
        ('jia-guangdong-telecom', 'http://119.134.202.156:8889/rtp/239.77.0.129:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.91:5999'),
    ],
    'CCTV-2': [
        ('iptvorg-74', 'http://74.91.26.218:82/live/cctv2hd.m3u8'),
        ('xisohi-bfgd', 'http://120.76.248.139/live/bfgd/4200000061.m3u8'),
        ('xisohi-jilin-ts', 'http://222.169.85.8:9901/tsfile/live/0002_1.m3u8'),
        ('xisohi-hls-112', 'http://112.46.85.60:8009/hls/502/index.m3u8'),
        ('xisohi-shandong-ts', 'http://58.57.40.22:9901/tsfile/live/1001_1.m3u8'),
        ('xisohi-kankan', 'https://play.kankanlive.com/live/1698234898628961.m3u8'),
        ('xisohi-hls-183', 'http://183.11.239.36:808/hls/20/index.m3u8'),
        ('jia-beijing-unicom', 'http://114.243.99.254:8888/rtp/239.3.1.60:8084'),
        ('jia-guangdong-telecom', 'http://119.134.202.156:8889/rtp/239.77.0.137:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.11:5999'),
    ],
    'CCTV-11': [
        ('iptvorg-74', 'http://74.91.26.218:82/live/cctv11hd.m3u8'),
        ('iptvorg-xykt', 'https://xykt-fix.github.io/play/a02b/index.m3u8'),
        ('xisohi-ts-61', 'http://61.136.172.236:9901/tsfile/live/0011_1.m3u8'),
        ('xisohi-kankan', 'https://play.kankanlive.com/live/1698423476198918.m3u8'),
        ('xisohi-hls-183', 'http://183.11.239.36:808/hls/29/index.m3u8'),
        ('jia-beijing-unicom', 'http://114.243.99.254:8888/rtp/239.3.1.152:8120'),
        ('jia-guangdong-telecom', 'http://119.134.202.156:8889/rtp/239.77.0.40:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.14:5999'),
    ],
    'CCTV-12': [
        ('iptvorg-74', 'http://74.91.26.218:82/live/cctv12hd.m3u8'),
        ('xisohi-hls-112-27', 'http://112.27.235.94:8000/hls/13/index.m3u8'),
        ('xisohi-ts-61', 'http://61.136.172.236:9901/tsfile/live/0012_1.m3u8'),
        ('xisohi-ts-59', 'http://59.39.89.130:60901/tsfile/live/0012_1.m3u8?key=txiptv&playlive=1&authid=0'),
        ('xisohi-shandong-ts', 'http://58.57.40.22:9901/tsfile/live/1012_1.m3u8'),
        ('xisohi-kankan', 'https://play.kankanlive.com/live/1698423511884917.m3u8'),
        ('xisohi-hls-183', 'http://183.11.239.36:808/hls/30/index.m3u8'),
        ('xisohi-hls-112-46', 'http://112.46.85.60:8009/hls/507/index.m3u8'),
        ('jia-beijing-unicom', 'http://114.243.99.254:8888/rtp/239.3.1.64:8124'),
        ('jia-guangdong-telecom', 'http://119.134.202.156:8889/rtp/239.77.0.41:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.15:5999'),
    ],
    'CCTV-14': [
        ('iptvorg-74', 'http://74.91.26.218:82/live/cctv14hd.m3u8'),
        ('xisohi-ts-61', 'http://61.136.172.236:9901/tsfile/live/0014_1.m3u8'),
        ('xisohi-shandong-ts', 'http://58.57.40.22:9901/tsfile/live/1014_1.m3u8'),
        ('xisohi-kankan', 'https://play.kankanlive.com/live/1698423575756915.m3u8'),
        ('xisohi-hebtv', 'https://event.pull.hebtv.com/jishi/cp2.m3u8'),
        ('jia-beijing-unicom', 'http://114.243.99.254:8888/rtp/239.3.1.65:8132'),
        ('jia-guangdong-telecom', 'http://119.134.202.156:8889/rtp/239.77.0.43:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.17:5999'),
    ],
    'CCTV-17': [
        ('iptvorg-74', 'http://74.91.26.218:82/live/cctv17hd.m3u8'),
        ('xisohi-hls-183', 'http://183.11.239.36:808/hls/35/index.m3u8'),
        ('xisohi-shandong-ts', 'http://58.57.40.22:9901/tsfile/live/0019_1.m3u8'),
        ('xisohi-kankan', 'https://play.kankanlive.com/live/1698423272597921.m3u8'),
        ('xisohi-hmfs', 'http://hmfs.f3322.net:3388/hls/37/index.m3u8'),
        ('jia-beijing-unicom', 'http://114.243.99.254:8888/rtp/239.3.1.151:8144'),
        ('jia-guangdong-telecom-a', 'http://119.134.202.156:8889/rtp/239.77.0.115:5146'),
        ('jia-guangdong-telecom-b', 'http://119.134.202.156:8889/rtp/239.77.0.198:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.137:5999'),
    ],
    'CCTV-4K': [
        ('iptvorg-198', 'http://198.204.240.250:82/live/cctv4k.m3u8'),
        ('xisohi-livephp', 'http://101.35.240.114:88/live.php?id=CCTV4K'),
        ('jia-guangdong-telecom-a', 'http://119.134.202.156:8889/rtp/239.77.0.194:5146'),
        ('jia-guangdong-telecom-b', 'http://119.134.202.156:8889/rtp/239.77.1.180:5146'),
        ('jia-guizhou-telecom', 'http://218.86.143.57:9527/rtp/238.255.2.139:5999'),
    ],
}


def score_hd(r) -> tuple:
    min_dl = min(r.download_mbps, r.second_download_mbps or r.download_mbps)
    headroom = min_dl / r.stream_mbps if r.stream_mbps else 0.0
    passed = bool(
        r.ok and r.width >= 1920 and r.height >= 1080 and
        r.stream_mbps >= 2.8 and min_dl >= max(6.0, r.stream_mbps * 1.35) and
        r.startup_s <= 2.5
    )
    return passed, min_dl, headroom


def score_4k(r) -> tuple:
    min_dl = min(r.download_mbps, r.second_download_mbps or r.download_mbps)
    headroom = min_dl / r.stream_mbps if r.stream_mbps else 0.0
    passed = bool(
        r.ok and r.width >= 3840 and r.height >= 2160 and
        r.stream_mbps >= 8.0 and min_dl >= max(15.0, r.stream_mbps * 1.30) and
        r.startup_s <= 3.0
    )
    return passed, min_dl, headroom


def one(item):
    channel, label, url = item
    r = probe(f'{channel}:{label}', url)
    d = asdict(r)
    passed, min_dl, headroom = score_4k(r) if channel == 'CCTV-4K' else score_hd(r)
    d.update(
        channel=channel,
        candidate=label,
        passed=passed,
        min_download_mbps=round(min_dl, 3),
        headroom=round(headroom, 3),
    )
    return d


def main() -> int:
    flat = [(ch, label, url) for ch, rows in CANDIDATES.items() for label, url in rows]
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as ex:
        rows = list(ex.map(one, flat))

    best = {}
    by_channel = {}
    for ch in CANDIDATES:
        group = [x for x in rows if x['channel'] == ch]
        group.sort(key=lambda x: (
            x['passed'], x['height'], x['width'], x['stream_mbps'],
            x['min_download_mbps'], -x['startup_s']
        ), reverse=True)
        by_channel[ch] = group
        best[ch] = next((x for x in group if x['passed']), None)

    payload = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'policy_hd': {'min': '1920x1080', 'min_stream_mbps': 2.8, 'min_download_mbps': 6.0, 'min_headroom': 1.35, 'max_startup_s': 2.5},
        'policy_4k': {'min': '3840x2160', 'min_stream_mbps': 8.0, 'min_download_mbps': 15.0, 'min_headroom': 1.30, 'max_startup_s': 3.0},
        'best_passing': best,
        'results': by_channel,
    }
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    for ch in CANDIDATES:
        b = best[ch]
        if b:
            print(f"BEST {ch}: {b['candidate']} {b['width']}x{b['height']} stream={b['stream_mbps']:.2f}Mbps min_dl={b['min_download_mbps']:.2f}Mbps startup={b['startup_s']:.2f}s")
        else:
            print(f'BEST {ch}: NONE')
        for x in by_channel[ch][:5]:
            state = 'PASS' if x['passed'] else ('OK' if x['ok'] else 'FAIL')
            print(f"  {state:4} {x['candidate']:24} {x['width']}x{x['height']} stream={x['stream_mbps']:.2f} dl={x['min_download_mbps']:.2f} startup={x['startup_s']:.2f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
