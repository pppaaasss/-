#!/usr/bin/env python3
"""Collapse generated CCTV/mainland-satellite duplicate tiles to one station.

The source-selection stage may test many IPv4/IPv6 routes, but the published
playlist must expose only one tile per station. This final guard also cleans
older carried-forward playlists if a broad rebuild is safety-stopped.
"""

from __future__ import annotations

import re
from pathlib import Path

PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf.strip()


def station_key(name: str) -> str | None:
    clean = re.sub(r"\s+(?:IPv[46](?:主线|备用)?|主线|备用\s*\d*(?:\s*1080p\d*)?|1080p50)$", "", name, flags=re.I).strip()
    if re.search(r"cctv[\s_-]*4[\s_-]*k", clean, re.I):
        return "cctv4k"
    if re.search(r"cctv[\s_-]*0?5\s*(?:\+|plus|p)", clean, re.I):
        return "cctv5plus"
    match = re.search(r"cctv[\s_-]*0?(\d{1,2})(?!\d)", clean, re.I)
    if match and 1 <= int(match.group(1)) <= 17:
        return f"cctv{int(match.group(1))}"
    sat = re.match(r"^(.+?卫视)(?:\s|$)", clean)
    if sat:
        return re.sub(r"\s+", "", sat.group(1))
    return None


def preference(name: str) -> tuple[int, int]:
    low = name.lower()
    # Plain canonical tile wins. If an old reinforced list is being rescued,
    # prefer its IPv4 fallback over an unverified cross-operator IPv6 tile.
    tagged = bool(re.search(r"ipv[46]|主线|备用", low, re.I))
    if not tagged:
        return (3, 0)
    if "ipv4" in low:
        return (2, 0)
    if "ipv6" in low:
        return (1, 0)
    return (0, 0)


def clean(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    header: list[str] = []
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and i + 1 < len(lines) and lines[i + 1].startswith(("http://", "https://")):
            entries.append((line, lines[i + 1]))
            i += 2
            continue
        header.append(line)
        i += 1

    best: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    passthrough: list[tuple[str, str]] = []
    removed = 0
    for extinf, url in entries:
        name = visible_name(extinf)
        key = station_key(name)
        if key is None:
            passthrough.append((extinf, url))
            continue
        if key not in best:
            best[key] = (extinf, url)
            order.append(key)
            continue
        old_extinf, _ = best[key]
        if preference(name) > preference(visible_name(old_extinf)):
            best[key] = (extinf, url)
        removed += 1

    output_entries: list[tuple[str, str]] = []
    used = set()
    for extinf, url in entries:
        key = station_key(visible_name(extinf))
        if key is None:
            output_entries.append((extinf, url))
        elif key not in used and best.get(key) == (extinf, url):
            output_entries.append((extinf, url))
            used.add(key)

    # Keep leading M3U/comments only; entry-adjacent metadata is not generated
    # by this project and is intentionally ignored by the normal builder too.
    leading = []
    for line in header:
        if not leading and not line.startswith("#"):
            continue
        leading.append(line)
    rendered = leading[:]
    for extinf, url in output_entries:
        rendered.extend((extinf, url))
    path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8", newline="\n")
    return len(entries), removed


def main() -> int:
    for name in PLAYLISTS:
        total, removed = clean(Path(name))
        print(f"{name}: entries={total} duplicate_core_tiles_removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
