#!/usr/bin/env python3
"""Single production entry point for the APTV playlist rebuild.

The big builder owns source discovery, probing, health history and publication
selection. This module adds the viewer's source/quality policy, then runs the
small final passes in one fixed order.

Production priorities:
1. keep the scheduled backend alive even when one upstream contains junk,
2. keep a usable previous publication if a rebuild becomes obviously broken,
3. healthy 2160p/1080p high-bitrate routes,
4. healthy ordinary 1080p routes,
5. 720p only as an availability fallback when HD candidates are unusable.

Viewer-confirmed pins are intentionally NOT reapplied by this scheduled entry
point. They remain a manual hotfix tool. Scheduled rebuilds must be allowed to
fail over when a pinned source later dies.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

import build_playlist


ROOT = Path(__file__).resolve().parents[1]
PLAYLISTS = (
    ROOT / "tv-easy.m3u",
    ROOT / "tv.m3u",
    ROOT / "tv-all.m3u",
    ROOT / "tv-core.m3u",
)
MIN_PUBLICATION_COUNTS = {
    "tv-easy.m3u": 40,
    "tv.m3u": 250,
    "tv-all.m3u": 300,
    "tv-core.m3u": 20,
}

# best-fan already credits these three, while build_playlist.py already reads
# iptv-org, BurningC4, hujingguang/ChinaIPTV and ibert.me directly.
EXTRA_UPSTREAMS = [
    ("大陆", "https://raw.githubusercontent.com/mzky/checklist/master/itvlist.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt", False),
    ("大陆", "https://raw.githubusercontent.com/kaige-cai/live/main/live.m3u", False),
]

# Viewer feedback beats upstream labels. This URL is advertised as CCTV-13
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
    """Quarantine malformed or viewer-rejected candidates without aborting a run."""
    if user_rejected(channel):
        return -1_000_000.0
    try:
        return _ORIGINAL_STATIC_SCORE(channel)
    except (ValueError, UnicodeError):
        # Community lists occasionally contain impossible ports or malformed
        # URL components. One bad line must never take the four-hour backend
        # down with it.
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

    # Do not reward a fake/unsustainable HD route. A working 720p route remains
    # available as the final anti-black-screen fallback.
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

    build_playlist.channel_static_score = viewer_static_score
    build_playlist.core_route_score = viewer_core_route_score
    build_playlist.measured_score = viewer_measured_score


def snapshot_playlists() -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    for path in PLAYLISTS:
        if path.exists():
            snapshot[path] = path.read_text(encoding="utf-8", errors="ignore")
    return snapshot


def playlist_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("#EXTINF"))


def publication_is_sane(previous: dict[Path, str]) -> tuple[bool, str]:
    """Reject empty/truncated catastrophic publications and keep the last good set."""
    for path in PLAYLISTS:
        if not path.exists():
            return False, f"missing {path.name}"
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.lstrip().startswith("#EXTM3U"):
            return False, f"invalid header in {path.name}"
        count = playlist_count(text)
        floor = MIN_PUBLICATION_COUNTS[path.name]
        if count < floor:
            return False, f"{path.name} only has {count} channels (<{floor})"

        old = previous.get(path)
        if old:
            old_count = playlist_count(old)
            # A sudden >35% catalogue collapse is much more likely to be a
            # broken upstream/probe run than a legitimate overnight change.
            if old_count >= floor and count < int(old_count * 0.65):
                return False, f"{path.name} collapsed from {old_count} to {count} channels"
    return True, "ok"


def restore_playlists(previous: dict[Path, str]) -> None:
    for path, text in previous.items():
        path.write_text(text, encoding="utf-8", newline="\n")


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
    except Exception:
        # A future malformed source or unexpected parser bug must not terminate
        # the wrapper before it can preserve the last usable publication.
        print("ERROR: unexpected base-builder exception", flush=True)
        traceback.print_exc()
        return 90


def main() -> int:
    configure_builder()
    previous = snapshot_playlists()

    base_rc = run_base_builder()
    failures: list[str] = []
    if base_rc:
        failures.append(f"base builder ({base_rc})")

    steps = [
        ("Promote verified 1080p50/1080i50 routes", "upgrade_core_50hz.py", False),
        ("Keep one visible tile per station", "clean_single_station.py", True),
        ("Apply viewer region/profile policy", "apply_viewer_profile.py", True),
        ("Repair CCTV-5 / CCTV-5+ independently", "repair_cctv5.py", True),
        # Do not reapply manual living-room pins here: an automatic rebuild must
        # be free to fail over if yesterday's viewer-confirmed source is dead.
        ("Mirror best-fan pay channels without stream tests", "import_best_fan_pay_unchecked.py", True),
    ]
    for label, script, required in steps:
        if not run_python(label, script, required=required):
            failures.append(script)

    sane, reason = publication_is_sane(previous)
    if not sane:
        print(f"ERROR: publication guard rejected rebuild: {reason}", flush=True)
        restore_playlists(previous)
        failures.append("publication guard")

    if failures:
        print("\nPipeline kept a usable publication but reported failures: " + ", ".join(failures), flush=True)
        return 1
    print("\nPipeline completed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
