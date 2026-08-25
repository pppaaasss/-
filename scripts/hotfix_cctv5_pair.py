#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
PINNED = {
    "cctv5": "http://219.140.56.34:3333/tsfile/live/1005_1.m3u8",
    "cctv5plus": "http://219.140.56.34:3333/tsfile/live/0016_1.m3u8",
}

TECH_SUFFIX = re.compile(
    r"(?:\s*[-_/]?\s*(?:备用\s*IPv4|IPv4\s*备用|备用\s*IPv6|IPv6\s*备用|"
    r"IPv[46](?:主线|备用)?|主线|备用(?:\s*\d+)?(?:\s*1080p\d*)?|"
    r"1080[pi]?\s*50|50\s*FPS))+\s*$",
    re.I,
)


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf.strip()


def strip_suffix(name: str) -> str:
    old = None
    clean = name.strip()
    while old != clean:
        old = clean
        clean = TECH_SUFFIX.sub("", clean).strip()
    return clean


def station_key(name: str) -> str | None:
    clean = strip_suffix(name)
    if re.search(r"cctv[\s_-]*4[\s_-]*k", clean, re.I):
        return "cctv4k"
    if re.search(r"cctv[\s_-]*0?5\s*(?:\+|plus|p)", clean, re.I):
        return "cctv5plus"
    m = re.search(r"cctv[\s_-]*0?(\d{1,2})(?!\d)", clean, re.I)
    if m and 1 <= int(m.group(1)) <= 17:
        return f"cctv{int(m.group(1))}"
    sat = re.match(r"^(.+?卫视)(?:\s|$)", clean)
    if sat:
        return re.sub(r"\s+", "", sat.group(1))
    return None


def canonical_name(key: str, fallback: str) -> str:
    if key == "cctv4k":
        return "CCTV-4K"
    if key == "cctv5plus":
        return "CCTV-5+"
    m = re.fullmatch(r"cctv(\d{1,2})", key)
    if m:
        return f"CCTV-{int(m.group(1))}"
    if key:
        return key
    return strip_suffix(fallback)


def set_name(extinf: str, name: str) -> str:
    return extinf.rsplit(",", 1)[0] + "," + name if "," in extinf else extinf + "," + name


def process(path: Path) -> None:
    if not path.exists():
        return
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
        if not entries:
            if "IPv6主线 / 实测备用" in line:
                line = line.replace("IPv6主线 / 实测备用", "单台单源 / 流畅优先")
            header.append(line)
        i += 1

    out: list[tuple[str, str]] = []
    seen_core: set[str] = set()
    for extinf, url in entries:
        raw_name = visible_name(extinf)
        clean_name = strip_suffix(raw_name)
        key = station_key(clean_name)
        if key:
            if key in seen_core:
                continue
            seen_core.add(key)
            clean_name = canonical_name(key, clean_name)
            if key in PINNED:
                url = PINNED[key]
        extinf = set_name(extinf, clean_name)
        out.append((extinf, url))

    count = len(out)
    updated_header: list[str] = []
    replaced = False
    for line in header:
        if line.startswith("# channels="):
            updated_header.append(f"# channels={count}")
            replaced = True
        else:
            updated_header.append(line)
    if not replaced:
        updated_header.append(f"# channels={count}")

    rendered = updated_header[:]
    for extinf, url in out:
        rendered.extend((extinf, url))
    path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8", newline="\n")
    print(f"{path}: channels={count} CCTV5={PINNED['cctv5']} CCTV5+={PINNED['cctv5plus']}")


def main() -> int:
    for name in PLAYLISTS:
        process(Path(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
