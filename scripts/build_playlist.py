#!/usr/bin/env python3
"""Build APTV playlists with real HLS manifest and media-segment probes.

The main playlist (tv.m3u) favors fast 720p/1080p streams.  A broader
tv-all.m3u is kept as a fallback so that a transient probe failure does not
make a channel disappear completely.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


TARGET_STABLE = int(os.getenv("TARGET_STABLE", "200"))
TARGET_ALL = int(os.getenv("TARGET_ALL", "320"))
PROBE_WORKERS = int(os.getenv("PROBE_WORKERS", "28"))
PROBE_TIMEOUT = float(os.getenv("PROBE_TIMEOUT", "9"))
MAX_VARIANTS_PER_CHANNEL = int(os.getenv("MAX_VARIANTS_PER_CHANNEL", "8"))
MAX_PROBE_BYTES = 768 * 1024
TODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV playlist health-check/2.0)"

# Stable-list target distribution.  Missing groups are filled by the fastest
# remaining healthy streams, so the final list can still reach 200 entries.
GROUP_TARGETS = {
    "大陆": 55,
    "中文纪录": 8,
    "中文电影": 6,
    "中文付费": 8,
    "香港": 15,
    "澳门": 5,
    "台湾": 18,
    "新加坡": 10,
    "马来西亚": 10,
    "日本": 10,
    "韩国": 10,
    "纪录片": 15,
    "电影": 12,
    "新闻": 8,
    "娱乐": 6,
    "音乐": 4,
}

GROUP_ORDER = {name: i for i, name in enumerate(GROUP_TARGETS)}
CHINESE_GROUPS = {
    "大陆", "中文纪录", "中文电影", "中文付费", "香港", "澳门", "台湾", "新加坡", "马来西亚"
}

SOURCES = [
    # Broad public directory used as the baseline.
    ("大陆", "https://iptv-org.github.io/iptv/countries/cn.m3u", False),
    ("香港", "https://iptv-org.github.io/iptv/countries/hk.m3u", True),
    ("澳门", "https://iptv-org.github.io/iptv/countries/mo.m3u", True),
    ("台湾", "https://iptv-org.github.io/iptv/countries/tw.m3u", False),
    ("新加坡", "https://iptv-org.github.io/iptv/countries/sg.m3u", True),
    ("马来西亚", "https://iptv-org.github.io/iptv/countries/my.m3u", True),
    ("日本", "https://iptv-org.github.io/iptv/countries/jp.m3u", True),
    ("韩国", "https://iptv-org.github.io/iptv/countries/kr.m3u", True),
    ("纪录片", "https://iptv-org.github.io/iptv/categories/documentary.m3u", False),
    ("电影", "https://iptv-org.github.io/iptv/categories/movies.m3u", False),
    ("新闻", "https://iptv-org.github.io/iptv/categories/news.m3u", False),
    ("娱乐", "https://iptv-org.github.io/iptv/categories/entertainment.m3u", False),
    ("音乐", "https://iptv-org.github.io/iptv/categories/music.m3u", False),

    # Frequently refreshed Chinese IPv4 lists.  Multiple URLs for the same
    # channel are intentionally kept until *after* probing; the fastest healthy
    # variant wins instead of whichever URL happened to appear first.
    ("大陆", "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_cctv_status.m3u8", False),
    ("大陆", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_province_status.m3u8", False),
    ("中文付费", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay_status.m3u8", False),
    ("大陆", "https://m3u.ibert.me/fmml_itv.m3u", False),
    ("大陆", "https://m3u.ibert.me/fmml_index.m3u", False),
    ("大陆", "https://m3u.ibert.me/y_g.m3u", False),
    ("大陆", "https://m3u.ibert.me/cn.m3u", False),
    ("大陆", "https://m3u.ibert.me/cn_p.m3u", False),

    # An independent FTA collection supplies additional regional CDN variants.
    ("香港", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_hong_kong.m3u8", True),
    ("香港", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_hongkong.m3u8", True),
    ("澳门", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_macau.m3u8", True),
    ("台湾", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_taiwan.m3u8", True),
    ("日本", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_japan.m3u8", True),
    ("韩国", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_korea.m3u8", True),
]

# User-requested Chinese documentary/movie channels.  They are never lost:
# successful probes may promote them to tv.m3u; otherwise they remain in
# tv-all.m3u as explicit fallbacks.
EXTRAS = [
    ("求索纪录", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000010.m3u8?cdn=hnbblive"),
    ("求索科学", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000011.m3u8?cdn=hnbblive"),
    ("求索生活", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000008.m3u8?cdn=hnbblive"),
    ("求索动物", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000009.m3u8?cdn=hnbblive"),
    ("CHC影迷电影", "中文电影", "http://58.19.38.162:9901/tsfile/live/1004_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("CHC动作电影", "中文电影", "http://58.19.38.162:9901/tsfile/live/1005_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("CHC家庭影院", "中文电影", "http://58.19.38.162:9901/tsfile/live/1006_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("纪实科教8K", "中文纪录", "http://111.31.106.140/downflv.brtvcloud.com/8klive/8kliveok.m3u8"),
]

BLOCK_WORDS = [
    "shopping", "shop ", " shop", "qvc", "hsn", "jewelry", "jewellery",
    "teleshop", "home shopping", "shop lc", "gemporia", "购物", "購物", "珠宝", "珠寶", "导购", "導購",
]
LOW_VALUE = ["radio", "weather", "天气", "氣象", "parliament", "council", "assembly", "legislature"]
PREFERRED_HOSTS = [
    "akamaized.net", "akamaihd.net", "cloudfront.net", "alicdn.com", "myalicdn.com",
    "brtvcloud.com", "fastly", "cloudflare", "brightcove", "bcovlive", "rthk", "hoy.tv",
    "streamingfast.net", "cdn", "edge",
]
UNSTABLE_HOST_HINTS = ["zzy", "wwang", "qqff", ".xyz", ".top", ".pw", ".work", ".icu"]


@dataclass
class Channel:
    name: str
    extinf: str
    url: str
    group: str
    allow_geo: bool = False
    curated: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    static_score: float = 0.0
    probe: dict = field(default_factory=dict)


def fetch_text(url: str, timeout: float = 25, headers: dict[str, str] | None = None, limit: int = 2_000_000) -> str:
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "application/vnd.apple.mpegurl,*/*"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(limit).decode("utf-8", "ignore")


def parse_headers(extinf: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    ua = re.search(r'http-user-agent="([^"]+)"', extinf, re.I)
    ref = re.search(r'http-referrer="([^"]+)"', extinf, re.I)
    if ua:
        headers["User-Agent"] = ua.group(1)
    if ref:
        headers["Referer"] = ref.group(1)
    return headers


def parse_m3u(text: str, group: str, allow_geo: bool) -> list[Channel]:
    channels: list[Channel] = []
    extinf: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            extinf = line
            continue
        if line.startswith("#"):
            continue
        if extinf and line.startswith(("http://", "https://")):
            name = extinf.split(",", 1)[1].strip() if "," in extinf else "Unknown"
            low = f"{name} {extinf}".lower()
            if any(word in low for word in BLOCK_WORDS):
                extinf = None
                continue
            if "geo-blocked" in low and not allow_geo:
                extinf = None
                continue
            if ".mpd" in line.lower():
                extinf = None
                continue
            channels.append(Channel(name, extinf, line, group, allow_geo, False, parse_headers(extinf)))
            extinf = None
    return channels


def normalized_name(name: str) -> str:
    value = re.sub(r"\([^)]*(?:\d{3,4}[pi]|geo|not 24|hd|sd)[^)]*\)", "", name, flags=re.I)
    value = re.sub(r"\[[^]]*\]", "", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def channel_key(channel: Channel) -> str:
    """Canonical key used only after alternate URLs have been speed-tested."""
    tvg_id = re.search(r'tvg-id="([^"]+)"', channel.extinf, re.I)
    if tvg_id:
        value = re.sub(r"@.*$", "", tvg_id.group(1).lower())
        value = re.sub(r"\.(?:cn|hk|mo|tw|sg|my|jp|kr)$", "", value)
        value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)
        if value:
            return value
    value = normalized_name(channel.name)
    value = re.sub(r"cctv[\s_-]*0?(\d+)(?:[+p])?", r"cctv\1", value, flags=re.I)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)


def labelled_height(channel: Channel) -> int:
    low = f"{channel.name} {channel.extinf}".lower()
    if "8k" in low or "4320" in low:
        return 4320
    if "4k" in low or "2160" in low:
        return 2160
    for height in (1440, 1080, 720, 576, 540, 480, 360, 240, 180):
        if str(height) in low:
            return height
    return 0


def channel_static_score(channel: Channel) -> float:
    low = f"{channel.name} {channel.extinf}".lower()
    parsed = urllib.parse.urlsplit(channel.url)
    host = (parsed.hostname or "").lower()
    score = 0.0
    if channel.url.startswith("https://"):
        score += 26
    if any(token in host for token in PREFERRED_HOSTS):
        score += 42
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        score -= 24
    if parsed.port and parsed.port not in (80, 443):
        score -= 12
    if any(token in host for token in UNSTABLE_HOST_HINTS):
        score -= 28
    if "not 24/7" in low:
        score -= 18
    if any(token in low for token in LOW_VALUE):
        score -= 8
    height = labelled_height(channel)
    if height == 1080:
        score += 20
    elif height == 720:
        score += 13
    elif height > 1080:
        score += 9  # 4K/8K is sharp, but less suitable for a no-buffer main list.
    elif 0 < height < 720:
        score -= 16
    if channel.group in CHINESE_GROUPS:
        score += 13
    if re.search(r"[\u4e00-\u9fff]", channel.name):
        score += 7
    if channel.headers:
        score -= 8
    if channel.curated:
        score += 6
    return score


def variant_rows(text: str, base_url: str) -> list[tuple[int, int, int, str]]:
    rows: list[tuple[int, int, int, str]] = []
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
        width = int(resolution.group(1)) if resolution else 0
        height = int(resolution.group(2)) if resolution else 0
        rate = int(bandwidth.group(1)) if bandwidth else 0
        for candidate in lines[index + 1:]:
            if not candidate or candidate.startswith("#"):
                continue
            rows.append((width, height, rate, urllib.parse.urljoin(base_url, candidate)))
            break
    return rows


def choose_variant(rows: list[tuple[int, int, int, str]]) -> tuple[int, int, int, str] | None:
    if not rows:
        return None
    # Lock the health test to the best <=1080p representation.  APTV still gets
    # the original master URL and can adapt down if the user's route fluctuates.
    normal = [row for row in rows if 720 <= row[1] <= 1080 and (not row[2] or row[2] <= 14_000_000)]
    if normal:
        return max(normal, key=lambda row: (row[1], row[0], row[2]))
    under = [row for row in rows if row[1] and row[1] <= 1080]
    if under:
        return max(under, key=lambda row: (row[1], row[0], row[2]))
    return min(rows, key=lambda row: (row[1] or 99999, row[2] or 999999999))


def first_media_uri(text: str, base_url: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")) or not urllib.parse.urlsplit(line).scheme:
            return urllib.parse.urljoin(base_url, line)
    return None


def timed_read(url: str, headers: dict[str, str], limit: int, timeout: float) -> tuple[bytes, float, str]:
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    request_headers.update(headers)
    request_headers["Range"] = f"bytes=0-{limit - 1}"
    req = urllib.request.Request(url, headers=request_headers)
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        final_url = response.geturl()
        data = response.read(limit)
    return data, max(time.monotonic() - started, 0.001), final_url


def probe_once(channel: Channel, use_declared_headers: bool) -> dict:
    headers = channel.headers if use_declared_headers else {}
    manifest_bytes, manifest_seconds, final_url = timed_read(channel.url, headers, 512 * 1024, PROBE_TIMEOUT)
    text = manifest_bytes.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise ValueError("not_hls")

    rows = variant_rows(text, final_url)
    selected_variant = choose_variant(rows)
    width = height = bandwidth = 0
    media_url = final_url
    media_text = text
    media_seconds = manifest_seconds
    if selected_variant:
        width, height, bandwidth, media_url = selected_variant
        media_bytes, media_seconds, media_url = timed_read(media_url, headers, 512 * 1024, PROBE_TIMEOUT)
        media_text = media_bytes.decode("utf-8", "ignore")
        if "#EXTM3U" not in media_text:
            raise ValueError("bad_variant")

    segment_url = first_media_uri(media_text, media_url)
    if not segment_url:
        raise ValueError("no_live_segment")
    segment, segment_seconds, _ = timed_read(segment_url, headers, MAX_PROBE_BYTES, PROBE_TIMEOUT)
    if len(segment) < 16 * 1024:
        raise ValueError("short_segment")
    speed_mbps = len(segment) * 8 / segment_seconds / 1_000_000
    return {
        "ok": True,
        "manifest_s": round(manifest_seconds + (media_seconds if selected_variant else 0), 3),
        "segment_mbps": round(speed_mbps, 2),
        "segment_bytes": len(segment),
        "width": width,
        "height": height,
        "bandwidth": bandwidth,
        "header_required": use_declared_headers,
    }


def probe_channel(channel: Channel) -> dict:
    errors: list[str] = []
    attempts = [False]
    if channel.headers:
        attempts.append(True)
    for use_headers in attempts:
        try:
            return probe_once(channel, use_headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 451) and channel.allow_geo:
                return {"ok": False, "geo_restricted": True, "error": f"http_{exc.code}"}
            errors.append(f"http_{exc.code}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as exc:
            errors.append(type(exc).__name__ + ":" + str(exc)[:80])
    return {"ok": False, "error": errors[-1] if errors else "probe_failed"}


def is_stable(channel: Channel) -> bool:
    probe = channel.probe
    if probe.get("geo_restricted"):
        # Official regional CDN streams can be valid behind the user's HK/JP/SG
        # exit even when GitHub's US runner receives a geo error.
        return channel.allow_geo and channel.static_score >= 55 and labelled_height(channel) >= 720
    if not probe.get("ok") or probe.get("header_required"):
        return False
    height = probe.get("height") or labelled_height(channel)
    speed = float(probe.get("segment_mbps") or 0)
    latency = float(probe.get("manifest_s") or 99)
    bandwidth_mbps = float(probe.get("bandwidth") or 0) / 1_000_000
    required_speed = max(2.2, bandwidth_mbps * 1.25)
    if height and height < 720:
        return False
    if latency > 5.0 or speed < required_speed:
        return False
    parsed = urllib.parse.urlsplit(channel.url)
    raw_ip = bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parsed.hostname or ""))
    if channel.url.startswith("http://") and (raw_ip or parsed.port not in (None, 80)):
        return speed >= 5.0 and latency <= 3.5
    return True


def measured_score(channel: Channel) -> float:
    score = channel.static_score
    probe = channel.probe
    if probe.get("geo_restricted"):
        return score - 25
    if not probe.get("ok"):
        return -9999
    speed = min(float(probe.get("segment_mbps") or 0), 40)
    latency = float(probe.get("manifest_s") or 10)
    height = int(probe.get("height") or labelled_height(channel))
    score += speed * 2.2 - latency * 7
    if height == 1080:
        score += 25
    elif height == 720:
        score += 16
    elif height > 1080:
        score += 6
    elif height and height < 720:
        score -= 30
    return score


def deduplicate(channels: Iterable[Channel]) -> list[Channel]:
    best_by_url: dict[str, Channel] = {}
    for channel in channels:
        channel.static_score = channel_static_score(channel)
        existing = best_by_url.get(channel.url)
        if existing is None or channel.static_score > existing.static_score:
            best_by_url[channel.url] = channel

    variants: dict[str, list[Channel]] = defaultdict(list)
    for channel in best_by_url.values():
        key = channel_key(channel)
        if key:
            variants[key].append(channel)

    result: list[Channel] = []
    for items in variants.values():
        items.sort(key=lambda item: (item.curated, item.static_score), reverse=True)
        result.extend(items[:MAX_VARIANTS_PER_CHANNEL])
    return result


def select_probe_pool(channels: list[Channel]) -> list[Channel]:
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in channels:
        grouped[channel.group].append(channel)
    pool: list[Channel] = []
    for group, items in grouped.items():
        quota = GROUP_TARGETS.get(group, 5)
        by_channel: dict[str, list[Channel]] = defaultdict(list)
        for item in items:
            by_channel[channel_key(item)].append(item)
        for variants in by_channel.values():
            variants.sort(key=lambda item: item.static_score, reverse=True)

        # Breadth first: test one URL for many different channels before using
        # remaining slots on alternate URLs for CCTV/卫视 and other duplicates.
        # This prevents eight CCTV-1 URLs from crowding seven other channels out.
        unique_limit = max(45, quota * 4)
        ranked_keys = sorted(
            by_channel,
            key=lambda key: by_channel[key][0].static_score,
            reverse=True,
        )[:unique_limit]
        group_pool = [by_channel[key][0] for key in ranked_keys]
        group_limit = max(80, quota * 6)
        variant_index = 1
        while len(group_pool) < group_limit:
            added = False
            for key in ranked_keys:
                variants = by_channel[key]
                if variant_index < len(variants):
                    group_pool.append(variants[variant_index])
                    added = True
                    if len(group_pool) >= group_limit:
                        break
            if not added:
                break
            variant_index += 1
        pool.extend(group_pool)
    # Curated entries must always be checked, even when their static score is low.
    for channel in channels:
        if channel.curated and channel not in pool:
            pool.append(channel)
    return pool


def select_stable(channels: list[Channel]) -> list[Channel]:
    # Keep the fastest measured URL for each actual channel.  URL alternatives
    # are useful during probing but must not become duplicate APTV tiles.
    best: dict[str, Channel] = {}
    for channel in channels:
        if not is_stable(channel):
            continue
        key = channel_key(channel)
        existing = best.get(key)
        if existing is None or measured_score(channel) > measured_score(existing):
            best[key] = channel
    eligible = list(best.values())
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in eligible:
        grouped[channel.group].append(channel)
    for items in grouped.values():
        items.sort(key=measured_score, reverse=True)

    selected: list[Channel] = []
    selected_urls: set[str] = set()
    for group, quota in GROUP_TARGETS.items():
        for channel in grouped.get(group, [])[:quota]:
            selected.append(channel)
            selected_urls.add(channel.url)

    if len(selected) < TARGET_STABLE:
        overflow = sorted(
            (channel for channel in eligible if channel.url not in selected_urls),
            key=measured_score,
            reverse=True,
        )
        selected.extend(overflow[: TARGET_STABLE - len(selected)])
    return selected[:TARGET_STABLE]


def select_all(channels: list[Channel], stable: list[Channel]) -> list[Channel]:
    selected = list(stable)
    urls = {channel.url for channel in selected}
    keys = {channel_key(channel) for channel in selected}
    curated = [
        channel for channel in channels
        if channel.curated and channel.url not in urls and channel_key(channel) not in keys
    ]
    for channel in curated:
        selected.append(channel)
        urls.add(channel.url)
        keys.add(channel_key(channel))
    best_remainder: dict[str, Channel] = {}
    for channel in channels:
        key = channel_key(channel)
        if channel.url in urls or key in keys:
            continue
        existing = best_remainder.get(key)
        rank = (is_stable(channel), measured_score(channel), channel.static_score)
        if existing is None or rank > (is_stable(existing), measured_score(existing), existing.static_score):
            best_remainder[key] = channel
    remainder = sorted(best_remainder.values(), key=lambda channel: (is_stable(channel), measured_score(channel), channel.static_score), reverse=True)
    selected.extend(remainder[: max(0, TARGET_ALL - len(selected))])
    return selected[:TARGET_ALL]


def cleaned_extinf(channel: Channel) -> str:
    extinf = channel.extinf
    if "group-title=" in extinf:
        extinf = re.sub(r'group-title="[^"]*"', f'group-title="{channel.group}"', extinf)
    else:
        extinf = extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{channel.group}"', 1)
    return extinf


def sort_channels(channels: list[Channel]) -> list[Channel]:
    return sorted(channels, key=lambda channel: (GROUP_ORDER.get(channel.group, 99), -measured_score(channel), channel.name.lower()))


def write_playlist(path: Path, channels: list[Channel], description: str) -> None:
    lines = [
        "#EXTM3U",
        f"# {description}",
        f"# generated_utc={TODAY}",
        f"# channels={len(channels)}",
    ]
    for channel in sort_channels(channels):
        lines.extend((cleaned_extinf(channel), channel.url))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    source_failures: list[str] = []
    candidates: list[Channel] = []
    for group, url, allow_geo in SOURCES:
        try:
            candidates.extend(parse_m3u(fetch_text(url), group, allow_geo))
        except Exception as exc:  # keep other source lists usable
            source_failures.append(f"{group}:{type(exc).__name__}:{str(exc)[:100]}")

    for name, group, url in EXTRAS:
        extinf = f'#EXTINF:-1 group-title="{group}",{name}'
        candidates.append(Channel(name, extinf, url, group, False, True, {}))

    candidates = deduplicate(candidates)
    probe_pool = select_probe_pool(candidates)
    print(f"candidates={len(candidates)} probe_pool={len(probe_pool)} workers={PROBE_WORKERS}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
        future_map = {executor.submit(probe_channel, channel): channel for channel in probe_pool}
        for completed, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            channel = future_map[future]
            try:
                channel.probe = future.result()
            except Exception as exc:
                channel.probe = {"ok": False, "error": type(exc).__name__ + ":" + str(exc)[:80]}
            if completed % 50 == 0:
                print(f"probed={completed}/{len(probe_pool)}")

    stable = select_stable(probe_pool)
    full = select_all(candidates, stable)
    healthy = sum(1 for channel in probe_pool if channel.probe.get("ok"))
    geo = sum(1 for channel in probe_pool if channel.probe.get("geo_restricted"))
    if len(stable) < min(120, TARGET_STABLE):
        raise SystemExit(f"safety stop: only {len(stable)} stable channels; existing tv.m3u was not replaced")

    write_playlist(Path("tv.m3u"), stable, "APTV 高清稳定版：实测 HLS 清单与视频分片；1080p/720p 优先")
    write_playlist(Path("tv-all.m3u"), full, "APTV 完整备用版：频道更多，未全部通过稳定性门槛")

    stable_groups = Counter(channel.group for channel in stable)
    stable_heights = Counter()
    for channel in stable:
        height = int(channel.probe.get("height") or labelled_height(channel))
        if height >= 2160:
            stable_heights["2160+"] += 1
        elif height >= 1080:
            stable_heights["1080"] += 1
        elif height >= 720:
            stable_heights["720"] += 1
        else:
            stable_heights["unlabelled_but_probed"] += 1
    errors = Counter(channel.probe.get("error", "") for channel in probe_pool if not channel.probe.get("ok"))
    report_lines = [
        f"generated_utc={TODAY}",
        f"source_candidates={len(candidates)}",
        f"probed={len(probe_pool)}",
        f"probe_healthy={healthy}",
        f"geo_restricted={geo}",
        f"stable_channels={len(stable)}",
        f"all_channels={len(full)}",
        f"stable_https={sum(channel.url.startswith('https://') for channel in stable)}",
        "stable_resolution=" + json.dumps(dict(stable_heights), ensure_ascii=False, sort_keys=True),
        "stable_groups=" + json.dumps(dict(stable_groups), ensure_ascii=False, sort_keys=True),
        "source_failures=" + json.dumps(source_failures, ensure_ascii=False),
        "top_probe_errors=" + json.dumps(errors.most_common(12), ensure_ascii=False),
    ]
    Path("build-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n".join(report_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
