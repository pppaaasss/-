#!/usr/bin/env python3
"""Keep CCTV-5 and CCTV-5+ on separate, freshly verified public routes.

The broad builder measures many sources, but a route reachable from a GitHub
runner can still be dead on the viewer's network.  This final pass therefore
uses small, independently hosted rescue pools for the two sports channels,
checks a live HLS segment twice, and publishes exactly one tile per station.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV CCTV5 pair rescue/2.0)"
TIMEOUT = 7.0
READ_LIMIT = 512 * 1024

# This single host was hard-pinned for both stations and the viewer confirmed
# that both routes stopped playing on 2026-08-25.  Never preserve it as a
# last-known-good fallback.
DEAD_HOSTS = {"219.140.56.34"}

# Candidate order is intentional: prefer recent, viewer-friendly public HLS
# routes over raw runner speed.  The pools use unrelated hosts so one outage
# cannot remove CCTV-5 and CCTV-5+ together again.
CANDIDATES = {
    "cctv5": (
        "http://1.24.39.180:9003/hls/5/index.m3u8",
        "http://120.76.248.139/live/bfgd/4200000064.m3u8",
        "http://107.150.60.122/live/cctv5hd.m3u8",
        "http://207.56.13.146:81/cdnlive/cctv5.m3u8",
        "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub",
        "http://198.204.228.26/live/cctv5hd.m3u8",
        "http://74.91.26.218:82/live/cctv5hd.m3u8",
        "http://58.56.162.102:4466/newlive/live/hls/5/live.m3u8",
    ),
    "cctv5plus": (
        "http://107.150.60.122/live/cctv5p.m3u8",
        "http://207.56.13.146:81/cdnlive/cctv5p.m3u8",
        "http://198.204.228.26/live/cctv5p.m3u8",
        "http://74.91.26.218:82/live/cctv5p.m3u8",
        "http://38.75.136.137:98/gslb/dsdqpub/cctv5p.m3u8?auth=testpub",
        "http://69.30.246.194/live/cctv5p.m3u8",
        "http://173.208.212.130:8181/720p/cctv5p.m3u8",
        "http://183.129.255.66:8480/hls/6/index.m3u8",
    ),
}

FALLBACKS = {
    "cctv5": CANDIDATES["cctv5"][0],
    "cctv5plus": CANDIDATES["cctv5plus"][0],
}

CANONICAL_NAMES = {"cctv5": "CCTV-5", "cctv5plus": "CCTV-5+"}


@dataclass(frozen=True)
class Probe:
    station: str
    url: str
    ok: bool
    mbps: float = 0.0
    error: str = ""

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class Selection:
    url: str
    reason: str
    probe: Probe | None = None


def fetch(url: str, limit: int = READ_LIMIT) -> tuple[bytes, str, float]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept": "application/vnd.apple.mpegurl,*/*",
            "Range": f"bytes=0-{limit - 1}",
        },
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(limit)
        return data, response.geturl(), max(time.monotonic() - started, 0.001)


def choose_variant(text: str, base_url: str) -> str | None:
    rows: list[tuple[int, int, str]] = []
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
        height = int(resolution.group(2)) if resolution else 0
        rate = int(bandwidth.group(1)) if bandwidth else 0
        for candidate in lines[index + 1 :]:
            if candidate and not candidate.startswith("#"):
                rows.append((height, rate, urllib.parse.urljoin(base_url, candidate)))
                break
    if not rows:
        return None
    normal = [row for row in rows if 540 <= row[0] <= 1080]
    return max(normal or rows, key=lambda row: (row[0], row[1]))[2]


def first_media_uri(text: str, base_url: str) -> str | None:
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if line and not line.startswith("#"):
            return urllib.parse.urljoin(base_url, line)
    return None


def probe_once(url: str) -> float:
    manifest, final_url, _ = fetch(url)
    text = manifest.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise ValueError("not_hls")

    variant = choose_variant(text, final_url)
    if variant:
        manifest, final_url, _ = fetch(variant)
        text = manifest.decode("utf-8", "ignore")
        if "#EXTM3U" not in text:
            raise ValueError("bad_variant")

    upper = text.upper().replace(" ", "")
    if "#EXT-X-ENDLIST" in upper or "#EXT-X-PLAYLIST-TYPE:VOD" in upper:
        raise ValueError("vod_playlist")
    segment_url = first_media_uri(text, final_url)
    if not segment_url:
        raise ValueError("no_segment")
    segment, _, seconds = fetch(segment_url)
    if len(segment) < 32 * 1024:
        raise ValueError("short_segment")
    return len(segment) * 8 / seconds / 1_000_000


def probe_candidate(station: str, url: str) -> Probe:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in DEAD_HOSTS:
        return Probe(station, url, False, error="viewer_confirmed_dead_host")
    path = urllib.parse.urlsplit(url).path.lower()
    if station == "cctv5" and re.search(r"cctv5(?:p|plus)", path, re.I):
        return Probe(station, url, False, error="wrong_station_variant")
    try:
        first = probe_once(url)
        second = probe_once(url)
        return Probe(station, url, True, min(first, second))
    except Exception as exc:  # Public regional routes fail normally.
        return Probe(station, url, False, error=f"{type(exc).__name__}:{str(exc)[:100]}")


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def station_key(extinf: str) -> str | None:
    value = visible_name(extinf).lower().replace("－", "-")
    if re.search(r"cctv\s*-?\s*0?5\s*(?:\+|plus|p)(?:\s|$)", value, re.I):
        return "cctv5plus"
    if re.fullmatch(r"cctv\s*-?\s*0?5(?:\s+备用\d+(?:\s+1080p)?)?", value, re.I):
        return "cctv5"
    return None


def parse_playlist(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    header: list[str] = []
    entries: list[tuple[str, str]] = []
    pending: str | None = None
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            pending = line
            started = True
        elif pending and line.startswith(("http://", "https://")):
            entries.append((pending, line))
            pending = None
        elif not started:
            header.append(raw)
    return header, entries


def with_name(extinf: str, name: str) -> str:
    return extinf.rsplit(",", 1)[0] + "," + name if "," in extinf else extinf + "," + name


def default_extinf(station: str) -> str:
    if station == "cctv5plus":
        return (
            '#EXTINF:-1 tvg-logo="https://live.fanmingming.cn/tv/CCTV5+.png" '
            'tvg-name="CCTV5+" tvg-id="CCTV5+" group-title="卫视台",CCTV-5+'
        )
    return (
        '#EXTINF:-1 tvg-logo="https://live.fanmingming.cn/tv/CCTV5.png" '
        'tvg-name="CCTV5" tvg-id="CCTV5" group-title="卫视台",CCTV-5'
    )


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


def existing_routes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    _, entries = parse_playlist(path.read_text(encoding="utf-8", errors="ignore"))
    routes: dict[str, str] = {}
    for extinf, url in entries:
        key = station_key(extinf)
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if key and key not in routes and host not in DEAD_HOSTS:
            routes[key] = url
    return routes


def choose_routes(results: list[Probe], previous: dict[str, str]) -> dict[str, Selection]:
    by_pair = {(probe.station, probe.url): probe for probe in results}
    chosen: dict[str, Selection] = {}
    used_hosts: set[str] = set()
    for station in ("cctv5", "cctv5plus"):
        healthy = [
            by_pair[(station, url)]
            for url in CANDIDATES[station]
            if by_pair[(station, url)].ok
        ]
        route = next((probe for probe in healthy if probe.host not in used_hosts), None)
        route = route or (healthy[0] if healthy else None)
        if route:
            selection = Selection(route.url, "fresh_probe", route)
        else:
            old = previous.get(station, "")
            old_host = (urllib.parse.urlsplit(old).hostname or "").lower()
            if old and old_host not in DEAD_HOSTS:
                selection = Selection(old, "previous_publish")
            else:
                selection = Selection(FALLBACKS[station], "curated_fallback")
        chosen[station] = selection
        host = (urllib.parse.urlsplit(selection.url).hostname or "").lower()
        if host:
            used_hosts.add(host)
    return chosen


def rewrite_playlist(path: Path, selections: dict[str, Selection]) -> bool:
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8", errors="ignore")
    header, entries = parse_playlist(original)
    templates: dict[str, str] = {}
    for extinf, _ in entries:
        key = station_key(extinf)
        if key and key not in templates:
            templates[key] = extinf

    output: list[tuple[str, str]] = []
    emitted: set[str] = set()
    for extinf, url in entries:
        key = station_key(extinf)
        if key not in selections:
            output.append((extinf, url))
            continue
        if key not in emitted:
            template = templates.get(key) or default_extinf(key)
            output.append((with_name(template, CANONICAL_NAMES[key]), selections[key].url))
            emitted.add(key)

    missing = [key for key in ("cctv5", "cctv5plus") if key not in emitted]
    if missing:
        additions = [
            (default_extinf(key), selections[key].url)
            for key in missing
        ]
        output[0:0] = additions

    header = update_count_header(header, len(output))
    rendered = "\n".join(header + [part for entry in output for part in entry]).rstrip() + "\n"
    if rendered == original:
        return False
    path.write_text(rendered, encoding="utf-8", newline="\n")
    print(
        f"{path}: CCTV-5={selections['cctv5'].url} "
        f"CCTV-5+={selections['cctv5plus'].url}"
    )
    return True


def update_report(selections: dict[str, Selection]) -> None:
    path = Path("build-report.txt")
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"^cctv5_(?:rescue_routes|pair_rescue)=.*\n?", "", text, flags=re.M)
    payload = {
        station: {
            "url": selected.url,
            "selection": selected.reason,
            "runner_ok": bool(selected.probe and selected.probe.ok),
            "runner_mbps": round(selected.probe.mbps, 2) if selected.probe else 0.0,
        }
        for station, selected in selections.items()
    }
    path.write_text(
        text.rstrip("\n")
        + "\ncctv5_pair_rescue="
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    jobs = [
        (station, url)
        for station, urls in CANDIDATES.items()
        for url in urls
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = [executor.submit(probe_candidate, station, url) for station, url in jobs]
        results = [future.result() for future in futures]

    for result in results:
        state = "ok" if result.ok else f"fail:{result.error}"
        print(f"probe {result.station} {result.url} -> {state} {result.mbps:.2f} Mbps")

    previous = existing_routes(Path("tv-easy.m3u"))
    selections = choose_routes(results, previous)
    for station, selected in selections.items():
        print(f"selected {station}={selected.url} reason={selected.reason}")

    for filename in PLAYLISTS:
        rewrite_playlist(Path(filename), selections)
    update_report(selections)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
