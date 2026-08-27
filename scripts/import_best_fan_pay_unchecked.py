#!/usr/bin/env python3
"""Mirror best-fan's pay-channel list into the APTV playlists without probing.

The upstream project already refreshes and orders cn_pay.m3u8.  This script only
fetches that playlist, keeps the first route for each visible channel name, and
publishes those entries as 中文付费.  It intentionally does *not* open, time,
ffprobe, or otherwise test any stream URL.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

UPSTREAM = "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay.m3u8"
PLAYLISTS = (Path("tv-easy.m3u"), Path("tv.m3u"), Path("tv-all.m3u"))
DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV unchecked pay mirror/1.0)"
MARKER = "# best-fan cn_pay.m3u8 unchecked mirror (no stream probing)"


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def name_key(name: str) -> str:
    value = re.sub(r"\([^)]*\)", "", name)
    value = re.sub(r"\[[^]]*\]", "", value)
    value = re.sub(r"\s+", "", value).lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value)


def force_pay_group(extinf: str) -> str:
    if re.search(r'group-title="[^"]*"', extinf, re.I):
        return re.sub(r'group-title="[^"]*"', 'group-title="中文付费"', extinf, flags=re.I)
    if extinf.startswith("#EXTINF"):
        head, sep, tail = extinf.partition(",")
        return f'{head} group-title="中文付费"{sep}{tail}'
    return extinf


def fetch_upstream() -> list[tuple[str, str, str]]:
    req = urllib.request.Request(
        UPSTREAM,
        headers={"User-Agent": DEFAULT_UA, "Accept": "application/vnd.apple.mpegurl,*/*"},
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        text = response.read(1_000_000).decode("utf-8", "ignore")

    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    extinf: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            extinf = line
            continue
        if extinf and line.startswith(("http://", "https://")):
            name = visible_name(extinf)
            key = name_key(name)
            if key and key not in seen:
                seen.add(key)
                entries.append((key, force_pay_group(extinf), line))
            extinf = None
        elif line and not line.startswith("#"):
            extinf = None

    # Structural sanity only.  This is not a stream-health test; on a broken or
    # truncated upstream fetch, preserve the currently published pay channels.
    if len(entries) < 5:
        raise RuntimeError(f"upstream pay list looked incomplete: {len(entries)} unique channels")
    return entries


def parse_blocks(text: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    prefix: list[str] = []
    blocks: list[tuple[str, str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and i + 1 < len(lines) and lines[i + 1].strip().startswith(("http://", "https://")):
            name = visible_name(line)
            blocks.append((name_key(name), line, lines[i + 1].strip()))
            i += 2
            continue
        # Preserve playlist header/comments, but drop an old mirror marker so
        # every run writes exactly one current marker.
        if line.strip() != MARKER:
            prefix.append(line)
        i += 1
    return prefix, blocks


def patch_playlist(path: Path, upstream: list[tuple[str, str, str]]) -> int:
    if not path.exists():
        return 0
    original = path.read_text(encoding="utf-8", errors="ignore")
    prefix, blocks = parse_blocks(original)
    managed = {key for key, _, _ in upstream}

    # The upstream pay set is authoritative for matching visible names.  Remove
    # older copies from any group first, then append one current upstream route.
    kept = [(key, extinf, url) for key, extinf, url in blocks if key not in managed]

    output: list[str] = []
    # Keep #EXTM3U and generated comments at the top.
    output.extend(prefix)
    if output and output[-1] != "":
        output.append("")
    output.append(MARKER)
    for _, extinf, url in kept:
        output.extend((extinf, url))
    for _, extinf, url in upstream:
        output.extend((extinf, url))

    rendered = "\n".join(output).rstrip() + "\n"
    if rendered != original:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return len(upstream)


def main() -> int:
    try:
        upstream = fetch_upstream()
    except Exception as exc:  # Preserve existing playlists if the index itself is unavailable.
        print(f"best-fan unchecked pay import skipped: {exc}")
        return 0

    for path in PLAYLISTS:
        count = patch_playlist(path, upstream)
        print(f"{path}: mirrored {count} unchecked best-fan pay channels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
