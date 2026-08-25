#!/usr/bin/env python3
"""Reinforce mainland CCTV/satellite channels with IPv6-first, IPv4-backup routes.

GitHub-hosted runners cannot reliably probe literal IPv6 television endpoints, so
this pass deliberately treats several independently maintained IPv6 playlists as
an upstream health signal. It only promotes direct literal IPv6 URLs for CCTV
and mainland satellite channels, while preserving the builder's freshly measured
IPv4 route as a visible fallback.

The normal builder remains the authority for HLS/segment/bitrate checks. This
script is intentionally conservative: no Hong Kong/Macau/Taiwan channels, no
fuzzy non-core promotion, and no replacement when there is no trusted IPv6
candidate.
"""

from __future__ import annotations

import collections
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV core IPv6 reinforcement/1.0)"
TIMEOUT = 12.0

UPSTREAMS = (
    ("fanmingming", "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u", 30),
    ("xjt1995", "https://raw.githubusercontent.com/xjt1995/iptv/main/cn.txt", 28),
    ("guovin", "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv6/result.m3u", 24),
    ("ftindy", "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/IPV6.m3u", 20),
    ("peterhchina", "https://raw.githubusercontent.com/peterHchina/iptv/main/CNTV-V6.m3u", 16),
)

MAINLAND_SATELLITES = {
    "三沙卫视", "东南卫视", "东方卫视", "云南卫视", "兵团卫视", "内蒙古卫视", "农林卫视",
    "北京卫视", "吉林卫视", "四川卫视", "天津卫视", "宁夏卫视", "安多卫视", "安徽卫视",
    "山东卫视", "山西卫视", "广东卫视", "广西卫视", "延边卫视", "新疆卫视", "江苏卫视",
    "江西卫视", "河北卫视", "河南卫视", "浙江卫视", "海南卫视", "海峡卫视", "深圳卫视",
    "湖北卫视", "湖南卫视", "甘肃卫视", "西藏卫视", "贵州卫视", "辽宁卫视", "重庆卫视",
    "陕西卫视", "青海卫视", "黑龙江卫视",
}

GENERATED_MARKERS = ("IPv6主线", "IPv6备用", "IPv4备用")


@dataclass
class Candidate:
    channel: str
    url: str
    sources: set[str] = field(default_factory=set)
    source_weight: int = 0

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").lower()

    @property
    def score(self) -> int:
        score = self.source_weight + max(0, len(self.sources) - 1) * 40
        low = self.url.lower()
        host = self.host
        if host.startswith("2409:8087:"):
            score += 18
        if any(token in low for token in ("/pltv/", "/cms001/", "/zte_cms/", "/tvod/")):
            score += 10
        if any(token in low for token in ("accountinfo=", "securitykey=", "timestamp=", "auth_key=", "token=")):
            score -= 24
        if "love=freedom" in low:
            score += 4
        return score


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA, "Accept": "text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read(8 * 1024 * 1024).decode("utf-8", "ignore")


def is_literal_ipv6(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def strip_quality(name: str) -> str:
    name = name.strip().replace("－", "-").replace("＋", "+")
    name = re.sub(r"[「【\[(（]\s*IPV?6\s*[」】\])）]", "", name, flags=re.I)
    name = re.sub(r"\b(?:2160p|1080p|1080i|720p|576p|540p|4k|uhd|fhd|hd)\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -_")
    return name


def canonical_name(name: str) -> str | None:
    raw = strip_quality(name)
    compact = re.sub(r"[\s_-]+", "", raw).lower()
    if re.search(r"cctv5\+|cctv5plus", compact, re.I):
        return "CCTV-5+"
    match = re.search(r"cctv(1[0-7]|[1-9])(?:\D|$)", compact, re.I)
    if match:
        number = int(match.group(1))
        if number == 5:
            return "CCTV-5"
        return f"CCTV-{number}"
    if "cctv4k" in compact:
        return "CCTV-4K"
    for satellite in MAINLAND_SATELLITES:
        if satellite in raw:
            return satellite
    return None


def parse_source(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    pending_name: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending_name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            continue
        if pending_name and line.startswith(("http://", "https://")):
            entries.append((pending_name, line))
            pending_name = None
            continue
        if line.startswith("#"):
            continue
        if "," in line:
            name, url = line.split(",", 1)
            url = url.strip()
            if url.startswith(("http://", "https://")):
                entries.append((name.strip(), url))
    return entries


def collect_candidates() -> tuple[dict[str, list[Candidate]], dict[str, str]]:
    by_key_url: dict[tuple[str, str], Candidate] = {}
    feed_status: dict[str, str] = {}
    for source, url, weight in UPSTREAMS:
        try:
            text = fetch_text(url)
            parsed = parse_source(text)
            accepted = 0
            for name, stream_url in parsed:
                channel = canonical_name(name)
                if channel is None or channel == "CCTV-5" or not is_literal_ipv6(stream_url):
                    continue
                key = (channel, stream_url)
                candidate = by_key_url.get(key)
                if candidate is None:
                    candidate = Candidate(channel=channel, url=stream_url)
                    by_key_url[key] = candidate
                candidate.sources.add(source)
                candidate.source_weight = max(candidate.source_weight, weight)
                accepted += 1
            feed_status[source] = f"ok:{accepted}"
        except Exception as exc:
            feed_status[source] = f"fail:{type(exc).__name__}:{str(exc)[:80]}"

    grouped: dict[str, list[Candidate]] = collections.defaultdict(list)
    for candidate in by_key_url.values():
        grouped[candidate.channel].append(candidate)
    for candidates in grouped.values():
        candidates.sort(key=lambda item: (item.score, len(item.sources)), reverse=True)
    return dict(grouped), feed_status


def select_distinct(candidates: list[Candidate], limit: int = 2) -> list[Candidate]:
    chosen: list[Candidate] = []
    used_hosts: set[str] = set()
    for candidate in candidates:
        if candidate.host in used_hosts:
            continue
        chosen.append(candidate)
        used_hosts.add(candidate.host)
        if len(chosen) >= limit:
            break
    if not chosen and candidates:
        chosen.append(candidates[0])
    return chosen


def parse_playlist(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    header: list[str] = []
    entries: list[tuple[str, str]] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            pending = line
        elif pending and line.startswith(("http://", "https://")):
            entries.append((pending, line))
            pending = None
        elif pending is None:
            header.append(raw)
    return header, entries


def display_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def with_display_name(extinf: str, name: str) -> str:
    if "," not in extinf:
        return extinf + "," + name
    return extinf.rsplit(",", 1)[0] + "," + name


def update_count_header(header: list[str], count: int) -> list[str]:
    output: list[str] = []
    replaced = False
    for line in header:
        if line.startswith("# channels="):
            output.append(f"# channels={count}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"# channels={count}")
    return output


def clean_previous_generated(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    cleaned: list[tuple[str, str]] = []
    for extinf, url in entries:
        visible = display_name(extinf)
        if any(marker in visible for marker in GENERATED_MARKERS):
            continue
        cleaned.append((extinf, url))
    return cleaned


def reinforce_playlist(path: Path, candidates: dict[str, list[Candidate]]) -> tuple[bool, int]:
    if not path.exists():
        return False, 0
    original = path.read_text(encoding="utf-8", errors="ignore")
    header, entries = parse_playlist(original)
    entries = clean_previous_generated(entries)

    first_index: dict[str, int] = {}
    for index, (extinf, _) in enumerate(entries):
        channel = canonical_name(display_name(extinf))
        if channel and channel not in first_index:
            first_index[channel] = index

    output: list[tuple[str, str]] = []
    promoted = 0
    for index, (extinf, url) in enumerate(entries):
        channel = canonical_name(display_name(extinf))
        if not channel or first_index.get(channel) != index or channel == "CCTV-5":
            output.append((extinf, url))
            continue
        options = select_distinct(candidates.get(channel, []), 2)
        if not options:
            output.append((extinf, url))
            continue

        output.append((with_display_name(extinf, f"{channel} IPv6主线"), options[0].url))
        if len(options) > 1:
            output.append((with_display_name(extinf, f"{channel} IPv6备用"), options[1].url))
        if url != options[0].url and (len(options) < 2 or url != options[1].url):
            family = "IPv6" if is_literal_ipv6(url) else "IPv4"
            output.append((with_display_name(extinf, f"{channel} {family}备用"), url))
        promoted += 1

    header = update_count_header(header, len(output))
    rendered = "\n".join(header + [part for entry in output for part in entry]) + "\n"
    if rendered == original:
        return False, promoted
    path.write_text(rendered, encoding="utf-8", newline="\n")
    return True, promoted


def build_core_playlist(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    _, entries = parse_playlist(source.read_text(encoding="utf-8", errors="ignore"))
    core = [(extinf, url) for extinf, url in entries if canonical_name(display_name(extinf))]
    header = [
        '#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"',
        '# 央视 + 中国大陆卫视：IPv6主线 / IPv6备用 / IPv4实测兜底',
        f'# channels={len(core)}',
    ]
    target.write_text("\n".join(header + [part for entry in core for part in entry]) + "\n", encoding="utf-8", newline="\n")
    return len(core)


def append_report(feed_status: dict[str, str], promoted: int, core_count: int) -> None:
    path = Path("build-report.txt")
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"^core_ipv6_.*\n?", "", text, flags=re.M)
    lines = [
        f"core_ipv6_promoted={promoted}",
        f"core_ipv6_playlist_channels={core_count}",
        "core_ipv6_feed_status=" + json.dumps(feed_status, ensure_ascii=False, sort_keys=True),
    ]
    path.write_text(text.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    candidates, feed_status = collect_candidates()
    promoted_total = 0
    for path in (Path("tv.m3u"), Path("tv-all.m3u")):
        changed, promoted = reinforce_playlist(path, candidates)
        promoted_total += promoted
        print(f"{path}: changed={changed} promoted={promoted}")

    core_count = build_core_playlist(Path("tv.m3u"), Path("tv-core.m3u"))
    append_report(feed_status, promoted_total, core_count)
    print("feeds=" + json.dumps(feed_status, ensure_ascii=False, sort_keys=True))
    print(f"core_playlist_channels={core_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
