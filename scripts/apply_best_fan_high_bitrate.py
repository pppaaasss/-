#!/usr/bin/env python3
"""Apply user-verified best-fan HD routes as the final publication override.

The broad builder runs from an overseas GitHub runner, so its throughput score
can prefer a small overseas transcode over a high-bitrate domestic IPTV route
that is fast on the viewer's actual connection.  This final pass trusts the
metadata-rich best-fan status list for same-name 1080p/2160p channels and only
replaces their URL.  Channel count, names, groups, logos and ordering stay
unchanged.
"""

from __future__ import annotations

import argparse
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import build_playlist as builder
except ModuleNotFoundError:  # Imported by the repository's offline tests.
    from scripts import build_playlist as builder


BEST_FAN_STATUS_URL = (
    "https://raw.githubusercontent.com/best-fan/iptv-sources/"
    "main/cn_all_status.m3u8"
)
PLAYLISTS = (
    Path("tv-easy.m3u"),
    Path("tv.m3u"),
    Path("tv-all.m3u"),
    Path("tv-core.m3u"),
)
DEFAULT_MIN_HEIGHT = 1080
DEFAULT_UA = "Mozilla/5.0 (APTV best-fan high-bitrate override/1.0)"


@dataclass(frozen=True)
class PreferredRoute:
    key: str
    name: str
    url: str
    height: int
    rank: tuple[int, int, int, int]


def override_key(channel: builder.Channel) -> str:
    """Keep regional CCTV-4 services separate from the mainland CCTV-4 tile."""
    visible = builder.normalized_name(channel.name)
    if re.search(r"cctv[\s_-]*4.*(?:欧洲|歐洲|美洲|europe|america)", visible, re.I):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", visible)
    return builder.channel_key(channel)


def read_source(location: str, timeout: float = 25.0) -> str:
    if location.startswith(("http://", "https://")):
        request = urllib.request.Request(location, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(2_000_000).decode("utf-8", "ignore")
    return Path(location).read_text(encoding="utf-8", errors="ignore")


def transport_rank(url: str) -> int:
    """Prefer original operator transport streams over small HD transcodes."""
    value = urllib.parse.unquote(url).lower()
    if re.search(r"(?:8m|8000000|10m|10000000|4k10m)", value):
        return 5
    if "/tsfile/live/" in value:
        return 4
    if "/newlive/live/hls/" in value or "/hls/" in value:
        return 3
    if "/3m1080p/" in value:
        return 1
    if "/720p/" in value or "dsdqbv" in value:
        return 0
    return 2


def preferred_routes(text: str, min_height: int = DEFAULT_MIN_HEIGHT) -> dict[str, PreferredRoute]:
    """Select one highest-quality best-fan route for each logical channel."""
    channels = builder.parse_m3u(text, "大陆", False, source=BEST_FAN_STATUS_URL)
    preferred: dict[str, PreferredRoute] = {}
    for index, channel in enumerate(channels):
        if not builder.is_station_like(channel) or builder.is_placeholder_relay(channel):
            continue
        key = override_key(channel)
        height = builder.labelled_height(channel)
        if not key or height < min_height:
            continue
        # Resolution and raw delivery format are quality signals. Upstream
        # order is the last tie-breaker; no remote stream speed test is run.
        rank = (height, transport_rank(channel.url), int(channel.url.startswith("https://")), -index)
        route = PreferredRoute(key, channel.name, channel.url, height, rank)
        if key not in preferred or rank > preferred[key].rank:
            preferred[key] = route
    return preferred


def split_entries(text: str) -> tuple[list[str], list[list[str]]]:
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("#EXTINF")), len(lines))
    header = lines[:first]
    entries: list[list[str]] = []
    index = first
    while index < len(lines):
        if not lines[index].startswith("#EXTINF"):
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("#EXTINF"):
            end += 1
        entries.append(lines[index:end])
        index = end
    return header, entries


def entry_channel(lines: list[str]) -> builder.Channel | None:
    parsed = builder.parse_m3u("\n".join(lines), "现有订阅", False)
    return parsed[0] if parsed else None


def replace_entry_url(lines: list[str], replacement: str) -> tuple[list[str], bool]:
    output = list(lines)
    for index in range(1, len(output)):
        if output[index].strip().startswith(("http://", "https://")):
            changed = output[index].strip() != replacement
            if changed:
                output[index] = replacement
            return output, changed
    return output, False


def render(header: list[str], entries: list[list[str]]) -> str:
    lines = list(header)
    for entry in entries:
        lines.extend(entry)
    return "\n".join(lines).rstrip() + "\n"


def apply_overrides(path: Path, routes: dict[str, PreferredRoute]) -> dict:
    if not path.exists():
        return {"path": str(path), "missing": True, "matched": 0, "changed": 0}
    original = path.read_text(encoding="utf-8", errors="ignore")
    header, entries = split_entries(original)
    matched = 0
    changed = 0
    names: list[str] = []
    output: list[list[str]] = []
    for entry in entries:
        channel = entry_channel(entry)
        route = routes.get(override_key(channel)) if channel else None
        if route is None:
            output.append(entry)
            continue
        matched += 1
        replaced, did_change = replace_entry_url(entry, route.url)
        output.append(replaced)
        if did_change:
            changed += 1
            names.append(builder.canonical_display_name(channel))
    rendered = render(header, output)
    if rendered != original:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return {
        "path": str(path),
        "entries": len(entries),
        "matched": matched,
        "changed": changed,
        "changed_names": names,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=BEST_FAN_STATUS_URL)
    parser.add_argument("--min-height", type=int, default=DEFAULT_MIN_HEIGHT)
    parser.add_argument("playlists", nargs="*", type=Path, default=list(PLAYLISTS))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        source_text = read_source(args.source)
        routes = preferred_routes(source_text, args.min_height)
    except Exception as exc:
        # A temporary upstream-index failure must not erase or rewrite the
        # already-published user-tested routes.
        print(f"best-fan override skipped: {type(exc).__name__}: {str(exc)[:160]}")
        return 0
    print(f"best_fan_preferred_routes={len(routes)} min_height={args.min_height}")
    for path in args.playlists:
        print(apply_overrides(path, routes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
