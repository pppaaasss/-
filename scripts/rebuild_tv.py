#!/usr/bin/env python3
"""Single production entry point for the APTV playlist rebuild.

The big builder owns source discovery, probing, health history and publication
selection.  This module adds the viewer's source/quality policy, then runs the
small final passes in one fixed order so GitHub Actions no longer has several
independent workflows fighting over the same channels.

Quality policy for ordinary tested channels:
1. healthy 2160p/1080p high-bitrate routes,
2. healthy ordinary 1080p routes,
3. 720p only as an availability fallback when HD candidates are unusable.

GitHub download throughput is not allowed to beat resolution/picture bitrate:
the original measured score remains useful for health/headroom, but the viewer
quality tier is deliberately much larger.  A nominal HD stream that cannot
sustain its own programme bitrate receives no HD bonus.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import build_playlist


ROOT = Path(__file__).resolve().parents[1]

# best-fan already credits these three, while build_playlist.py already reads
# iptv-org, BurningC4, hujingguang/ChinaIPTV and ibert.me directly.
EXTRA_UPSTREAMS = [
    ("大陆", "https://raw.githubusercontent.com/mzky/checklist/master/itvlist.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt", False),
    ("大陆", "https://raw.githubusercontent.com/kaige-cai/live/main/live.m3u", False),
]

# Viewer feedback beats upstream labels.  This URL is advertised as CCTV-13
# 1080p by an upstream index but the living-room player actually receives 720p.
USER_REJECTED_ROUTES = {
    ("cctv13", "http://74.91.26.218:82/live/cctv13hd.m3u8"),
}

_ORIGINAL_STATIC_SCORE = build_playlist.channel_static_score
_ORIGINAL_CORE_ROUTE_SCORE = build_playlist.core_route_score
_ORIGINAL_MEASURED_SCORE = build_playlist.measured_score


def user_rejected(channel: build_playlist.Channel) -> bool:
    return (build_playlist.channel_key(channel), channel.url) in USER_REJECTED_ROUTES


def viewer_static_score(channel: build_playlist.Channel) -> float:
    """Keep malformed upstream URLs from crashing the whole scheduled rebuild."""
    if user_rejected(channel):
        return -1_000_000.0
    try:
        return _ORIGINAL_STATIC_SCORE(channel)
    except (ValueError, UnicodeError):
        # Some community lists occasionally contain impossible ports or other
        # malformed URL components. Treat them as unusable candidates instead
        # of letting one bad line stop all channel refreshes.
        return -1_000_000.0


def viewer_quality_bonus(channel: build_playlist.Channel) -> float:
    """Make real picture quality dominate ranking after a route passes probes."""
    if user_rejected(channel):
        return -1_000_000.0

    probe = channel.probe
    if not probe.get("ok") or probe.get("duplicate_core_content"):
        return 0.0

    height = int(probe.get("height") or build_playlist.labelled_height(channel) or 0)
    stream_mbps = float(probe.get("stream_mbps") or 0.0)
    if not stream_mbps:
        stream_mbps = float(probe.get("bandwidth") or 0.0) / 1_000_000
    speed = float(probe.get("segment_mbps") or 0.0)

    # Do not reward a fake/unsustainable HD route.  This keeps a working 720p
    # route available as the final anti-black-screen fallback.
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

    # Within one resolution tier, prefer the higher programme bitrate.  Cap it
    # so broken metadata cannot dominate forever.
    bitrate_quality = min(max(stream_mbps, 0.0), 12.0) * 120.0
    return tier + bitrate_quality


def viewer_core_route_score(channel: build_playlist.Channel) -> float:
    if user_rejected(channel):
        return -1_000_000.0
    return _ORIGINAL_CORE_ROUTE_SCORE(channel) + viewer_quality_bonus(channel)


def viewer_measured_score(channel: build_playlist.Channel) -> float:
    if user_rejected(channel):
        return -1_000_000.0
    return _ORIGINAL_MEASURED_SCORE(channel) + viewer_quality_bonus(channel)


def configure_builder() -> None:
    existing_urls = {url for _, url, _ in build_playlist.SOURCES}
    for spec in EXTRA_UPSTREAMS:
        if spec[1] not in existing_urls:
            build_playlist.SOURCES.append(spec)
            existing_urls.add(spec[1])

    # Patch ranking only. All existing live-manifest/media-segment health gates
    # remain authoritative. The static wrapper also quarantines malformed URLs
    # so one broken community-list entry cannot abort the entire four-hour run.
    build_playlist.channel_static_score = viewer_static_score
    build_playlist.core_route_score = viewer_core_route_score
    build_playlist.measured_score = viewer_measured_score


def run_python(label: str, script: str, *, required: bool = True) -> bool:
    print(f"\n=== {label} ===", flush=True)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script)],
        cwd=ROOT,
        check=False,
    )
    if result.returncode == 0:
        return True
    level = "ERROR" if required else "WARNING"
    print(f"{level}: {script} exited {result.returncode}", flush=True)
    return not required


def run_base_builder() -> int:
    print("\n=== Build and probe all upstreams ===", flush=True)
    try:
        return int(build_playlist.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


def main() -> int:
    configure_builder()

    # Preserve the old behaviour: even if the broad family safety gate fails,
    # run rescue/finalisation passes so the last usable publication can still
    # be repaired and committed.  The caller may mark the overall run red later.
    base_rc = run_base_builder()
    failures: list[str] = []
    if base_rc:
        failures.append(f"base builder ({base_rc})")

    steps = [
        ("Promote verified 1080p50/1080i50 routes", "upgrade_core_50hz.py", False),
        ("Keep one visible tile per station", "clean_single_station.py", True),
        ("Apply viewer region/profile policy", "apply_viewer_profile.py", True),
        ("Repair CCTV-5 / CCTV-5+ independently", "repair_cctv5.py", True),
        ("Reapply viewer-confirmed living-room pins", "pin_viewer_channels.py", True),
        ("Mirror best-fan pay channels without stream tests", "import_best_fan_pay_unchecked.py", True),
    ]
    for label, script, required in steps:
        if not run_python(label, script, required=required):
            failures.append(script)

    if failures:
        print("\nPipeline completed with failures: " + ", ".join(failures), flush=True)
        return 1
    print("\nPipeline completed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
