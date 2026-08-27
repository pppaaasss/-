#!/usr/bin/env python3
"""Pin only the viewer-confirmed living-room routes.

This is intentionally surgical: it only replaces the URL for CCTV-1, CCTV-4,
CCTV-5, CCTV-6 and 湖南卫视. Channel metadata, ordering and every other station stay untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

# Touch this file to run the targeted pin workflow without rebuilding the catalogue.
# CCTV-1 uses the 221.7 route from the uploaded cn_all list; viewer cache is now 30s.
# CCTV-5 uses a previously verified 1080p rescue route; never downgrade it to 720p.
# Hunan now uses an operator-published 湖南卫视4K HLS candidate from the current
# Hunan Mobile list. The viewer will verify actual 3840x2160 and bitrate in APTV.
# CCTV-6 moves off the viewer-confirmed black-screen 222.169 route to the current
# first 1080-labelled best-fan candidate; actual APTV playback remains final authority.
PLAYLISTS = (
    Path("tv-easy.m3u"),
    Path("tv.m3u"),
    Path("tv-all.m3u"),
    Path("tv-core.m3u"),
)

TARGET_URLS = {
    "cctv1": "http://221.7.175.154:8445/tsfile/live/1000_1.m3u8?key=txiptv&playlive=1&authid=0",
    "cctv4": "http://221.7.175.154:8445/tsfile/live/1003_1.m3u8?key=txiptv&playlive=1&authid=0",
    "cctv5": "http://198.204.228.26/live/cctv5hd.m3u8",
    "cctv6": "http://112.30.73.119:9901/tsfile/live/0006_2.m3u8?key=txiptv&playlive=0&authid=0",
    "hunan": "http://tvgslb.hn.chinamobile.com:8089/180000001001/00000001000000000064000000308827/main.m3u8",
}


def target_key(extinf: str) -> str | None:
    if not extinf.startswith("#EXTINF") or "," not in extinf:
        return None
    name = extinf.rsplit(",", 1)[-1].strip()
    compact = re.sub(r"[\s_-]+", "", name).lower()
    if compact == "cctv1":
        return "cctv1"
    if compact in {"cctv4", "cctv4中文国际"}:
        return "cctv4"
    if compact == "cctv5":
        return "cctv5"
    if compact == "cctv6":
        return "cctv6"
    if name.strip() == "湖南卫视":
        return "hunan"
    return None


def patch_playlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    original = path.read_text(encoding="utf-8", errors="ignore")
    lines = original.splitlines()
    changed: list[str] = []
    pending: str | None = None

    for index, line in enumerate(lines):
        if line.startswith("#EXTINF"):
            pending = target_key(line)
            continue
        if pending and line.strip().startswith(("http://", "https://")):
            replacement = TARGET_URLS[pending]
            if line.strip() != replacement:
                lines[index] = replacement
                changed.append(pending)
            pending = None
        elif line.strip() and not line.startswith("#"):
            pending = None

    rendered = "\n".join(lines).rstrip() + "\n"
    if rendered != original:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return changed


def main() -> int:
    for path in PLAYLISTS:
        changed = patch_playlist(path)
        print(f"{path}: changed={','.join(changed) if changed else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
