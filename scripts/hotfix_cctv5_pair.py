#!/usr/bin/env python3
"""Fast, surgical hotfix for the three user-priority high-bitrate channels.

Only the stream URL immediately following the exact EXTINF entries for
CCTV-5, CCTV-5+ and 湖南卫视 is replaced.  Every other byte-level playlist
entry is left untouched by this script.
"""

from __future__ import annotations

from pathlib import Path

PLAYLISTS = (
    Path("tv-easy.m3u"),
    Path("tv.m3u"),
    Path("tv-all.m3u"),
    Path("tv-core.m3u"),
)

# High-bitrate 1080 sources. CCTV-5 uses the repository's measured 1080p50
# ~10 Mbps route; CCTV-5+ and Hunan use advertised 8 Mbps 1080 China Mobile
# CDN routes instead of the current low-bitrate fallbacks.
TARGETS = {
    "CCTV-5": "http://120.76.248.139/live/bfgd/4200000064.m3u8",
    "CCTV-5+": "http://otttv.bj.chinamobile.com/TVOD/88888888/224/3221226458/1.m3u8",
    "湖南卫视": "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221226307/index.m3u8",
}


def exact_display_name(extinf: str) -> str | None:
    if not extinf.startswith("#EXTINF:") or "," not in extinf:
        return None
    return extinf.rsplit(",", 1)[1].strip()


def patch_playlist(path: Path) -> int:
    if not path.exists():
        return 0

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    i = 0
    while i < len(lines):
        display = exact_display_name(lines[i].rstrip("\r\n"))
        replacement = TARGETS.get(display or "")
        if replacement:
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("#"):
                j += 1
            if j < len(lines):
                ending = "\r\n" if lines[j].endswith("\r\n") else "\n"
                current = lines[j].rstrip("\r\n")
                if current != replacement:
                    lines[j] = replacement + ending
                    changed += 1
            i = j
        i += 1

    if changed:
        path.write_text("".join(lines), encoding="utf-8", newline="")
    return changed


def main() -> int:
    total = 0
    for playlist in PLAYLISTS:
        changed = patch_playlist(playlist)
        total += changed
        print(f"{playlist}: changed={changed}")
    print(f"total_changed={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
