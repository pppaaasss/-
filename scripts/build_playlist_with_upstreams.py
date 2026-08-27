#!/usr/bin/env python3
"""Run the normal playlist builder with the remaining best-fan upstream pools.

The main builder already reads iptv-org, BurningC4, hujingguang/ChinaIPTV and
ibert.me directly, plus best-fan's generated lists.  best-fan also credits
mzky/checklist, ssili126/tv and kaige-cai/live; add those three here so every
scheduled rebuild can discover their current routes too.

These are ordinary candidate sources: unlike the separate cn_pay mirror, their
streams still pass through build_playlist.py's normal manifest/segment/quality
checks before they can be published.
"""

from __future__ import annotations

from scripts import build_playlist


EXTRA_UPSTREAMS = [
    ("大陆", "https://raw.githubusercontent.com/mzky/checklist/master/itvlist.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt", False),
    ("大陆", "https://raw.githubusercontent.com/kaige-cai/live/main/live.m3u", False),
]


def main() -> int:
    existing_urls = {url for _, url, _ in build_playlist.SOURCES}
    for spec in EXTRA_UPSTREAMS:
        if spec[1] not in existing_urls:
            build_playlist.SOURCES.append(spec)
            existing_urls.add(spec[1])
    return build_playlist.main()


if __name__ == "__main__":
    raise SystemExit(main())
