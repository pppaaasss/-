#!/usr/bin/env python3
"""Normalize final APTV presentation and keep the viewer-approved Hunan route.

User-facing policy:
- keep CCTV/satellites together in ``卫视台``;
- place 湖南卫视 immediately after the last CCTV tile;
- force 湖南卫视 to the viewer-approved working route;
- collapse every mainland province/city bucket into one ``地方台`` group;
- leave pay/HK/TW/JP and other specialty groups alone.

This is intentionally the final publication pass. The builder may keep
province-level groups internally for quotas and dead-source repair; the files
APTV reads must always expose the compact grouping requested by the viewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


PLAYLISTS = (
    Path("tv-easy.m3u"),
    Path("tv.m3u"),
    Path("tv-all.m3u"),
    Path("tv-core.m3u"),
)

HUNAN_STABLE_URL = (
    "http://112.123.243.37:50085/tsfile/live/1002_1.m3u8"
    "?key=txiptv&playlive=0&authid=0"
)

MAINLAND_REGION_GROUPS = {
    "北京", "上海", "天津", "重庆",
    "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西", "海南",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "其他地方",
}


@dataclass
class Block:
    extinf: str
    url: str


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def group_name(extinf: str) -> str:
    match = re.search(r'group-title="([^"]*)"', extinf, re.I)
    return match.group(1).strip() if match else ""


def set_group(extinf: str, group: str) -> str:
    if re.search(r'group-title="[^"]*"', extinf, re.I):
        return re.sub(
            r'group-title="[^"]*"',
            f'group-title="{group}"',
            extinf,
            count=1,
            flags=re.I,
        )
    return extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{group}"', 1)


def is_numbered_cctv(block: Block) -> bool:
    if group_name(block.extinf) != "卫视台":
        return False
    name = visible_name(block.extinf).replace(" ", "")
    return bool(re.match(r"^CCTV-?(?:\d{1,2}|4K)(?:$|[^A-Za-z0-9])", name, re.I))


def is_hunan_satellite(block: Block) -> bool:
    return group_name(block.extinf) == "卫视台" and visible_name(block.extinf).replace(" ", "") in {
        "湖南卫视", "湖南衛視"
    }


def parse_playlist(text: str) -> tuple[list[str], list[Block]]:
    misc: list[str] = []
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            line.startswith("#EXTINF")
            and i + 1 < len(lines)
            and lines[i + 1].strip().startswith(("http://", "https://"))
        ):
            blocks.append(Block(line, lines[i + 1].strip()))
            i += 2
            continue
        misc.append(line)
        i += 1
    return misc, blocks


def normalize_blocks(blocks: list[Block]) -> list[Block]:
    normalized: list[Block] = []
    for block in blocks:
        group = group_name(block.extinf)
        extinf = set_group(block.extinf, "地方台") if group in MAINLAND_REGION_GROUPS else block.extinf
        url = HUNAN_STABLE_URL if is_hunan_satellite(Block(extinf, block.url)) else block.url
        normalized.append(Block(extinf, url))

    # Stable move only: keep every other station in its current relative order.
    hunan = [block for block in normalized if is_hunan_satellite(block)]
    if not hunan:
        return normalized
    others = [block for block in normalized if not is_hunan_satellite(block)]
    last_cctv = max((i for i, block in enumerate(others) if is_numbered_cctv(block)), default=-1)
    insert_at = last_cctv + 1
    return others[:insert_at] + hunan + others[insert_at:]


def patch(path: Path) -> tuple[int, bool]:
    if not path.exists():
        return 0, False
    original = path.read_text(encoding="utf-8", errors="ignore")
    misc, blocks = parse_playlist(original)
    normalized = normalize_blocks(blocks)

    output = list(misc)
    while output and output[-1] == "":
        output.pop()
    for block in normalized:
        output.extend((block.extinf, block.url))
    rendered = "\n".join(output).rstrip() + "\n"
    changed = rendered != original
    if changed:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return len(normalized), changed


def main() -> int:
    for path in PLAYLISTS:
        count, changed = patch(path)
        print(f"{path}: channels={count} presentation_changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
