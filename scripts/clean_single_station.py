#!/usr/bin/env python3
"""Collapse generated CCTV/mainland-satellite tiles to one clean station entry.

Source selection may test IPv4, IPv6, 1080p50, 1080i50 and fallback routes,
but the published APTV playlist exposes exactly one tile with a plain station
name. Technical route details belong in reports, never in the living-room UI.
"""

from __future__ import annotations

import re
from pathlib import Path

PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf.strip()


def station_key(name: str) -> str | None:
    # Strip every generated/legacy route-quality suffix before identity matching.
    clean = re.sub(
        r"\s+(?:IPv[46](?:主线|备用)?|主线|备用\s*\d*(?:\s*1080p\d*)?|1080[pi]?\s*50|1080p50|1080i50|50\s*FPS)$",
        "",
        name,
        flags=re.I,
    ).strip()
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


def canonical_name(key: str) -> str:
    if key == "cctv4k":
        return "CCTV-4K"
    if key == "cctv5plus":
        return "CCTV-5+"
    match = re.fullmatch(r"cctv(\d{1,2})", key)
    if match:
        return f"CCTV-{int(match.group(1))}"
    return key


def canonicalize_extinf(extinf: str, key: str) -> str:
    """Keep EPG/logo metadata but make the visible APTV label plain."""
    name = canonical_name(key)
    if "," in extinf:
        return extinf.rsplit(",", 1)[0] + "," + name
    return extinf + "," + name


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


def clean(path: Path) -> tuple[int, int, int]:
    if not path.exists():
        return 0, 0, 0
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
    removed = 0
    for extinf, url in entries:
        name = visible_name(extinf)
        key = station_key(name)
        if key is None:
            continue
        if key not in best:
            best[key] = (extinf, url)
            continue
        old_extinf, _ = best[key]
        if preference(name) > preference(visible_name(old_extinf)):
            best[key] = (extinf, url)
        removed += 1

    output_entries: list[tuple[str, str]] = []
    used: set[str] = set()
    renamed = 0
    for extinf, url in entries:
        key = station_key(visible_name(extinf))
        if key is None:
            output_entries.append((extinf, url))
            continue
        if key in used or best.get(key) != (extinf, url):
            continue
        clean_extinf = canonicalize_extinf(extinf, key)
        if clean_extinf != extinf:
            renamed += 1
        output_entries.append((clean_extinf, url))
        used.add(key)

    # Keep generated M3U header/comments. Entry-adjacent directives are not
    # emitted by this project and are intentionally ignored, matching builder behavior.
    leading: list[str] = []
    for line in header:
        if not leading and not line.startswith("#"):
            continue
        leading.append(line)
    rendered = leading[:]
    for extinf, url in output_entries:
        rendered.extend((extinf, url))
    path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8", newline="\n")
    return len(entries), removed, renamed


def main() -> int:
    for name in PLAYLISTS:
        total, removed, renamed = clean(Path(name))
        print(
            f"{name}: entries={total} duplicate_core_tiles_removed={removed} "
            f"core_names_canonicalized={renamed}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
