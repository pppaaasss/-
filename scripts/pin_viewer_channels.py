#!/usr/bin/env python3
"""Pin only the three viewer-confirmed living-room routes.

This is intentionally surgical: it only replaces the URL for CCTV-1, CCTV-4
and 湖南卫视. Channel metadata, ordering and every other station stay untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

PLAYLISTS = (
    Path("tv-easy.m3u"),
    Path("tv.m3u"),
    Path("tv-all.m3u"),
    Path("tv-core.m3u"),
)

TARGET_URLS = {
    "cctv1": "http://221.7.175.154:8445/tsfile/live/1000_1.m3u8?key=txiptv&playlive=1&authid=0",
    "cctv4": "http://221.7.175.154:8445/tsfile/live/1003_1.m3u8?key=txiptv&playlive=1&authid=0",
    "hunan": "http://221.7.175.154:8445/tsfile/live/0128_1.m3u8?key=txiptv&playlive=1&authid=0",
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
