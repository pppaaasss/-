#!/usr/bin/env python3
"""Repair or remove dead published local/regional stations.

This is a post-build safety pass for the channels that tend to rot fastest:
mainland provincial/city/local stations.  It deliberately does NOT probe or
manage the unchecked pay-channel mirror.

Policy:
- keep a currently working local route (avoid pointless churn),
- call a route dead only after two fresh HLS manifest/media checks fail,
- for a dead tile, try same-name alternates from best-fan's fresh province/all
  status indexes,
- verify a replacement again before publishing it,
- remove the tile if no living alternate exists instead of leaving a black
  screen occupying APTV space.

The four-hour builder remains the main source selector.  This pass is the last
line of defence against stale local entries and does not rank by GitHub speed.
"""

from __future__ import annotations

import concurrent.futures
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


PLAYLISTS = (
    Path("tv-easy.m3u"),
    Path("tv.m3u"),
    Path("tv-all.m3u"),
)

CANDIDATE_FEEDS = (
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_province_status.m3u8",
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all_status.m3u8",
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_province.m3u8",
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all.m3u8",
)

LOCAL_GROUPS = {
    "北京", "上海", "天津", "重庆", "河北", "山西", "内蒙古", "辽宁", "吉林",
    "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
    "湖南", "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆", "其他地方",
}

DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV local-liveness-maintainer/1.0)"
TIMEOUT = 7.0
WORKERS = 40
MAX_ALTERNATES_PER_STATION = 6
MAX_MANIFEST_BYTES = 512 * 1024
MAX_SEGMENT_BYTES = 96 * 1024


@dataclass(frozen=True)
class Block:
    extinf: str
    url: str
    key: str
    group: str


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def name_key(name: str) -> str:
    value = re.sub(r"\([^)]*\)", "", name)
    value = re.sub(r"\[[^]]*\]", "", value)
    value = re.sub(r"(?:高清|超清|标清|hd|fhd|uhd|4k|1080p?|720p?)$", "", value, flags=re.I)
    value = re.sub(r"\s+", "", value).lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value)


def extinf_group(extinf: str) -> str:
    match = re.search(r'group-title="([^"]*)"', extinf, re.I)
    return match.group(1).strip() if match else ""


def parse_blocks(text: str) -> list[Block]:
    lines = text.splitlines()
    blocks: list[Block] = []
    i = 0
    while i + 1 < len(lines):
        if lines[i].startswith("#EXTINF") and lines[i + 1].strip().startswith(("http://", "https://")):
            extinf = lines[i]
            url = lines[i + 1].strip()
            blocks.append(Block(extinf, url, name_key(visible_name(extinf)), extinf_group(extinf)))
            i += 2
            continue
        i += 1
    return blocks


def fetch_bytes(url: str, limit: int, *, timeout: float = TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept": "application/vnd.apple.mpegurl,video/mp2t,video/*,*/*",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(limit)


def first_uri_after(lines: list[str], predicate) -> str | None:
    for index, raw in enumerate(lines):
        if not predicate(raw.strip()):
            continue
        for following in lines[index + 1 :]:
            line = following.strip()
            if not line or line.startswith("#"):
                continue
            return line
    return None


def media_playlist(url: str, depth: int = 0) -> tuple[str, str] | None:
    if depth > 2:
        return None
    raw = fetch_bytes(url, MAX_MANIFEST_BYTES)
    text = raw.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        return None
    lines = text.splitlines()

    # A master playlist: follow the first listed variant.  We only need a
    # liveness check here; quality selection is owned by the main builder.
    variant = first_uri_after(lines, lambda line: line.startswith("#EXT-X-STREAM-INF"))
    if variant:
        child = urllib.parse.urljoin(url, variant)
        return media_playlist(child, depth + 1)

    # Do not keep obvious VOD snapshots as a live TV replacement.
    if any(line.strip().startswith("#EXT-X-ENDLIST") for line in lines):
        return None

    segment = first_uri_after(lines, lambda line: line.startswith("#EXTINF"))
    if not segment:
        # Some feeds omit EXTINF in broken-but-playable manifests.  Accept a
        # media-looking URI as a fallback rather than false-killing the tile.
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if re.search(r"\.(?:ts|m4s|mp4|aac)(?:\?|$)", line, re.I):
                segment = line
                break
    if not segment:
        return None
    return url, urllib.parse.urljoin(url, segment)


def probe_once(url: str) -> bool:
    try:
        resolved = media_playlist(url)
        if not resolved:
            return False
        _, segment = resolved
        payload = fetch_bytes(segment, MAX_SEGMENT_BYTES)
        return len(payload) >= 1024
    except Exception:
        return False


def confirm_current(url: str) -> bool:
    if probe_once(url):
        return True
    # One remote timeout must not evict a working station.
    time.sleep(0.12)
    return probe_once(url)


def fetch_candidates() -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = defaultdict(list)
    seen_urls: dict[str, set[str]] = defaultdict(set)
    for feed in CANDIDATE_FEEDS:
        try:
            text = fetch_bytes(feed, 2_500_000, timeout=20).decode("utf-8", "ignore")
        except Exception as exc:
            print(f"candidate feed skipped: {feed}: {type(exc).__name__}")
            continue
        for block in parse_blocks(text):
            if not block.key or block.url in seen_urls[block.key]:
                continue
            seen_urls[block.key].add(block.url)
            candidates[block.key].append(block.url)
    return candidates


def local_blocks_by_url() -> tuple[dict[Path, str], dict[str, list[Block]]]:
    texts: dict[Path, str] = {}
    by_url: dict[str, list[Block]] = defaultdict(list)
    for path in PLAYLISTS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        texts[path] = text
        for block in parse_blocks(text):
            if block.group in LOCAL_GROUPS and block.key:
                by_url[block.url].append(block)
    return texts, by_url


def choose_replacements(dead_blocks: list[Block], candidates: dict[str, list[str]]) -> dict[str, str | None]:
    dead_keys = sorted({block.key for block in dead_blocks})
    tasks: list[tuple[str, str]] = []
    current_urls = {block.url for block in dead_blocks}
    for key in dead_keys:
        added = 0
        for url in candidates.get(key, []):
            if url in current_urls:
                continue
            tasks.append((key, url))
            added += 1
            if added >= MAX_ALTERNATES_PER_STATION:
                break

    alive: dict[str, list[str]] = defaultdict(list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {executor.submit(probe_once, url): (key, url) for key, url in tasks}
        for future in concurrent.futures.as_completed(future_map):
            key, url = future_map[future]
            try:
                ok = bool(future.result())
            except Exception:
                ok = False
            if ok:
                alive[key].append(url)

    replacements: dict[str, str | None] = {}
    for key in dead_keys:
        # Preserve upstream order, not completion order.
        ordered = [url for url in candidates.get(key, []) if url in set(alive.get(key, []))]
        chosen = None
        for url in ordered:
            if url in current_urls:
                continue
            # Fresh second confirmation before a new route is committed.
            if probe_once(url):
                chosen = url
                break
        replacements[key] = chosen
    return replacements


def rewrite_playlist(path: Path, text: str, dead_urls: set[str], replacements: dict[str, str | None]) -> tuple[int, int]:
    lines = text.splitlines()
    output: list[str] = []
    replaced = 0
    removed = 0
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and lines[i].startswith("#EXTINF") and lines[i + 1].strip().startswith(("http://", "https://")):
            extinf = lines[i]
            url = lines[i + 1].strip()
            group = extinf_group(extinf)
            key = name_key(visible_name(extinf))
            if group in LOCAL_GROUPS and url in dead_urls:
                replacement = replacements.get(key)
                if replacement:
                    output.extend((extinf, replacement))
                    replaced += 1
                else:
                    removed += 1
                i += 2
                continue
        output.append(lines[i])
        i += 1

    rendered = "\n".join(output).rstrip() + "\n"
    if rendered != text:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return replaced, removed


def main() -> int:
    texts, by_url = local_blocks_by_url()
    if not by_url:
        print("local maintenance: no published local blocks found")
        return 0

    alive: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {executor.submit(confirm_current, url): url for url in by_url}
        for future in concurrent.futures.as_completed(future_map):
            url = future_map[future]
            try:
                alive[url] = bool(future.result())
            except Exception:
                alive[url] = False

    dead_urls = {url for url, ok in alive.items() if not ok}
    dead_blocks = [block for url in dead_urls for block in by_url[url]]
    print(
        f"local maintenance: unique_current={len(by_url)} alive={len(by_url)-len(dead_urls)} "
        f"dead={len(dead_urls)}"
    )
    if not dead_urls:
        return 0

    candidates = fetch_candidates()
    replacements = choose_replacements(dead_blocks, candidates)

    total_replaced = 0
    total_removed = 0
    for path, text in texts.items():
        replaced, removed = rewrite_playlist(path, text, dead_urls, replacements)
        total_replaced += replaced
        total_removed += removed
        print(f"{path}: local replaced={replaced} removed={removed}")

    replaced_keys = sum(1 for value in replacements.values() if value)
    removed_keys = sum(1 for value in replacements.values() if not value)
    print(
        f"local maintenance complete: station_keys_replaced={replaced_keys} "
        f"station_keys_without_live_alternate={removed_keys} blocks_replaced={total_replaced} "
        f"blocks_removed={total_removed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
