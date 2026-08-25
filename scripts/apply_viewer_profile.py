#!/usr/bin/env python3
"""Apply the living-room channel profile as a final publication guard.

Upstream playlists sometimes misfile English services under 中文综合.  This
script therefore removes both unwanted group titles and unwanted services by
identity, after all route promotion/de-duplication passes have finished.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
BLOCKED_GROUPS = {"纪录片", "中文纪录", "电影", "中文电影", "新闻", "国际", "韩国"}

ENGLISH_SERVICE_RE = re.compile(
    r"(?:(?<![a-z])(?:bbc|cnn|fox\s*news|nbc(?:lx|\s*news)?|cbs\s*news|abc\s*news|"
    r"bloomberg|cnbc|reuters|sky\s*news|euronews|i24news|voa|newsmax|"
    r"cheddar\s*news|channel\s*newsasia|al\s*jazeera|france\s*24)(?![a-z])|"
    r"\bdw(?:\s+(?:english|news))?\b|nhk\s*world|kbs\s*world|arirang|"
    r"taiwan\s*(?:\+|plus)|viutv\s*six|viutvsix|tvb\s*pearl|\bpearl\b|"
    r"明珠台|英语|英語|英文|(?<![a-z])cna(?![a-z])|hoy\s+international\s+business)",
    re.I,
)
CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
CORE_RE = re.compile(r"(?:\bcctv[\s_-]*(?:\d{1,2}|4k)|央视|衛視|卫视)", re.I)
REGIONAL_BRAND_RE = re.compile(
    r"\b(?:brtv|btv|cetv|jstv|gdtv|grt|tvb|rthk|hoy|viutv|"
    r"phoenix|tdm|tvbs|ttv|ctv|cts|ftv|set|ebc|momo|ntd)\b",
    re.I,
)
SINGAPORE_CHINESE_RE = re.compile(
    r"(?:channel\s*(?:8|u)\b|8\s*频道|8頻道|u\s*频道|u頻道|华语|華語|中文)",
    re.I,
)


@dataclass(frozen=True)
class Entry:
    extinf: str
    lines: tuple[str, ...]


def attribute(extinf: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', extinf, re.I)
    return match.group(1).strip() if match else ""


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf


def keep_entry(extinf: str) -> tuple[bool, str]:
    group = attribute(extinf, "group-title")
    identity = " ".join(
        part for part in (
            visible_name(extinf),
            attribute(extinf, "tvg-name"),
            attribute(extinf, "tvg-id"),
        ) if part
    )
    if group in BLOCKED_GROUPS:
        return False, f"group:{group}"
    if re.search(r"(?<![a-z])cgtn(?![a-z])", identity, re.I):
        return False, "service:CGTN"
    if ENGLISH_SERVICE_RE.search(identity):
        return False, "service:English"
    if CORE_RE.search(identity):
        return True, "core"
    if group == "新加坡":
        return (bool(SINGAPORE_CHINESE_RE.search(identity)), "region:Singapore-Chinese")
    if group in {"台湾", "日本"}:
        return True, f"region:{group}"
    if group == "香港":
        keep = bool(CJK_RE.search(identity) or REGIONAL_BRAND_RE.search(identity))
        return keep, "region:Hong-Kong-Chinese"
    if group == "澳门":
        keep = bool(CJK_RE.search(identity) or re.search(r"\btdm\b", identity, re.I))
        return keep, "region:Macau-Chinese"
    keep = bool(CJK_RE.search(identity) or REGIONAL_BRAND_RE.search(identity))
    return keep, "language:CJK" if keep else "service:non-CJK"


def split_playlist(text: str) -> tuple[list[str], list[Entry]]:
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("#EXTINF")), len(lines))
    header = lines[:first]
    entries: list[Entry] = []
    i = first
    while i < len(lines):
        if not lines[i].startswith("#EXTINF"):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith("#EXTINF"):
            j += 1
        entries.append(Entry(lines[i], tuple(lines[i:j])))
        i = j
    return header, entries


def render(header: list[str], entries: list[Entry]) -> str:
    output_header: list[str] = []
    count_written = False
    for line in header:
        if line.startswith("# channels="):
            output_header.append(f"# channels={len(entries)}")
            count_written = True
        else:
            output_header.append(line)
    if not count_written:
        output_header.append(f"# channels={len(entries)}")
    lines = output_header[:]
    for entry in entries:
        lines.extend(entry.lines)
    return "\n".join(lines).rstrip() + "\n"


def apply_profile(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "missing": True}
    original = path.read_text(encoding="utf-8", errors="ignore")
    header, entries = split_playlist(original)
    kept: list[Entry] = []
    removed = Counter()
    for entry in entries:
        keep, reason = keep_entry(entry.extinf)
        if keep:
            kept.append(entry)
        else:
            removed[reason] += 1
    rendered = render(header, kept)
    path.write_text(rendered, encoding="utf-8", newline="\n")

    # Publication guard: future edits must not silently weaken the profile.
    for entry in kept:
        keep, reason = keep_entry(entry.extinf)
        if not keep:
            raise RuntimeError(f"profile leak in {path}: {reason}: {entry.extinf}")
    return {
        "path": str(path),
        "before": len(entries),
        "after": len(kept),
        "removed": dict(sorted(removed.items())),
        "changed": rendered != original,
    }


def main() -> int:
    for filename in PLAYLISTS:
        print(apply_profile(Path(filename)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
