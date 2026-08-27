#!/usr/bin/env python3
"""Run the normal playlist builder with all credited upstream pools.

The main builder already reads iptv-org, BurningC4, hujingguang/ChinaIPTV and
ibert.me directly, plus best-fan's generated lists. best-fan also credits
mzky/checklist, ssili126/tv and kaige-cai/live; add those three here so every
scheduled rebuild can discover their current routes too.

Viewer policy for ordinary tested channels:
1. healthy 2160p/1080p high-bitrate routes,
2. healthy ordinary 1080p routes,
3. 720p only as a last-resort availability fallback.

The quality bonus is applied only after a route passed the builder's live
manifest/media checks. A nominal 1080p route that cannot sustain its own stream
bitrate gets no HD bonus, so a working 720p route can still prevent a black
screen when all 1080p candidates are effectively unusable.
"""

from __future__ import annotations

from scripts import build_playlist


EXTRA_UPSTREAMS = [
    ("大陆", "https://raw.githubusercontent.com/mzky/checklist/master/itvlist.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt", False),
    ("大陆", "https://raw.githubusercontent.com/kaige-cai/live/main/live.m3u", False),
]

_ORIGINAL_CORE_ROUTE_SCORE = build_playlist.core_route_score
_ORIGINAL_MEASURED_SCORE = build_playlist.measured_score


def _viewer_quality_bonus(channel: build_playlist.Channel) -> float:
    probe = channel.probe
    if not probe.get("ok") or probe.get("duplicate_core_content"):
        return 0.0

    height = int(probe.get("height") or build_playlist.labelled_height(channel) or 0)
    stream_mbps = float(probe.get("stream_mbps") or 0.0)
    if not stream_mbps:
        stream_mbps = float(probe.get("bandwidth") or 0.0) / 1_000_000
    speed = float(probe.get("segment_mbps") or 0.0)

    # Do not reward a nominal HD route that cannot actually carry its own
    # programme bitrate. This is what allows 720p to remain the emergency
    # fallback instead of publishing a high-resolution black/buffering tile.
    if stream_mbps and speed and speed < stream_mbps * 1.05:
        return 0.0

    if height >= 2160:
        tier = 12_000.0
    elif height >= 1080:
        tier = 10_000.0
    elif height >= 720:
        tier = 1_000.0
    else:
        tier = 0.0

    # Inside the same resolution tier, picture bitrate matters before excess
    # download speed. Cap it so a pathological metadata value cannot dominate.
    bitrate_quality = min(max(stream_mbps, 0.0), 12.0) * 120.0
    return tier + bitrate_quality


def _viewer_core_route_score(channel: build_playlist.Channel) -> float:
    return _ORIGINAL_CORE_ROUTE_SCORE(channel) + _viewer_quality_bonus(channel)


def _viewer_measured_score(channel: build_playlist.Channel) -> float:
    return _ORIGINAL_MEASURED_SCORE(channel) + _viewer_quality_bonus(channel)


def main() -> int:
    existing_urls = {url for _, url, _ in build_playlist.SOURCES}
    for spec in EXTRA_UPSTREAMS:
        if spec[1] not in existing_urls:
            build_playlist.SOURCES.append(spec)
            existing_urls.add(spec[1])

    # Patch only ranking, not the builder's health gates. Dead/black/unreadable
    # routes still fail the ordinary probe path and therefore cannot win merely
    # because their label says 1080p.
    build_playlist.core_route_score = _viewer_core_route_score
    build_playlist.measured_score = _viewer_measured_score
    return build_playlist.main()


if __name__ == "__main__":
    raise SystemExit(main())
