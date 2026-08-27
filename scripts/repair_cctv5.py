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
SEGMENT_SAMPLE_LIMIT = 1024 * 1024
VIEWER_CCTV5_1080_URL = "http://198.204.228.26/live/cctv5hd.m3u8"

# This single host was hard-pinned for both stations and the viewer confirmed
# that both routes stopped playing on 2026-08-25.  Never preserve it as a
# last-known-good fallback.
DEAD_HOSTS = {"219.140.56.34"}

# Candidate order is intentional: prefer recent, viewer-friendly public HLS
# routes over raw runner speed.  The pools use unrelated hosts so one outage
# cannot remove CCTV-5 and CCTV-5+ together again.
CANDIDATES = {
    "cctv5": (
        "http://207.56.13.146:81/cdnlive/cctv5.m3u8",
        "http://107.150.60.122/live/cctv5hd.m3u8",
        "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub",
        VIEWER_CCTV5_1080_URL,
        "http://1.24.39.180:9003/hls/5/index.m3u8",
        "http://120.76.248.139/live/bfgd/4200000064.m3u8",
        "http://58.56.162.102:4466/newlive/live/hls/5/live.m3u8",
    ),
    "cctv5plus": (
        "http://107.150.60.122/live/cctv5p.m3u8",
        "http://207.56.13.146:81/cdnlive/cctv5p.m3u8",
        "http://198.204.228.26/live/cctv5p.m3u8",
        "http://38.75.136.137:98/gslb/dsdqpub/cctv5p.m3u8?auth=testpub",
        "http://173.208.212.130:8181/720p/cctv5p.m3u8",
    ),
}

FALLBACKS = {
    "cctv5": VIEWER_CCTV5_1080_URL,
    "cctv5plus": CANDIDATES["cctv5plus"][0],
}

# The living-room player is the final resolution authority.  This route was
# confirmed there as 1080p, while the much faster 207.56 route displayed as
# 720p.  Prefer the confirmed route whenever it is still live and has at least
# modest playback headroom; only then fall back to the generic pair optimizer.
VIEWER_CONFIRMED_1080_ROUTES = {
    "cctv5": frozenset({VIEWER_CCTV5_1080_URL}),
    "cctv5plus": frozenset(),
}
VIEWER_CONFIRMED_MIN_HEADROOM = 1.15
VIEWER_CONFIRMED_MIN_STREAM_MBPS = 2.4

CANONICAL_NAMES = {"cctv5": "CCTV-5", "cctv5plus": "CCTV-5+"}


@dataclass(frozen=True)
class Probe:
    station: str
    url: str
    ok: bool
    mbps: float = 0.0
    stream_mbps: float = 0.0
    height: int = 0
    error: str = ""

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class Selection:
    url: str
    reason: str
    probe: Probe | None = None


def fetch(url: str, limit: int = READ_LIMIT) -> tuple[bytes, str, float, int]:
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
        total_size = 0
        content_range = response.headers.get("Content-Range", "")
        range_match = re.search(r"/([0-9]+)$", content_range)
        if range_match:
            total_size = int(range_match.group(1))
        elif getattr(response, "status", 200) != 206:
            content_length = response.headers.get("Content-Length", "")
            if content_length.isdigit():
                total_size = int(content_length)
        data = response.read(limit)
        total_size = max(total_size, len(data))
        return data, response.geturl(), max(time.monotonic() - started, 0.001), total_size


def choose_variant(text: str, base_url: str) -> tuple[str, int] | None:
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
    height, _, url = max(normal or rows, key=lambda row: (row[0], row[1]))
    return url, height


def first_media_segment(text: str, base_url: str) -> tuple[str | None, float]:
    duration = 0.0
    candidate: tuple[str, float] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF:"):
            match = re.search(r"#EXTINF:([0-9.]+)", line, re.I)
            duration = float(match.group(1)) if match else 0.0
        elif line and not line.startswith("#"):
            candidate = (urllib.parse.urljoin(base_url, line), duration)
    return candidate or (None, 0.0)


def probe_once(url: str) -> tuple[float, float, int]:
    manifest, final_url, _, _ = fetch(url)
    text = manifest.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise ValueError("not_hls")

    height = 0
    variant = choose_variant(text, final_url)
    if variant:
        variant_url, height = variant
        manifest, final_url, _, _ = fetch(variant_url)
        text = manifest.decode("utf-8", "ignore")
        if "#EXTM3U" not in text:
            raise ValueError("bad_variant")

    upper = text.upper().replace(" ", "")
    if "#EXT-X-ENDLIST" in upper or "#EXT-X-PLAYLIST-TYPE:VOD" in upper:
        raise ValueError("vod_playlist")
    segment_url, duration = first_media_segment(text, final_url)
    if not segment_url:
        raise ValueError("no_segment")
    # Read a bounded sample for throughput, while Content-Range/Length reveals
    # the complete media-object size for EXTINF-based programme bitrate. This
    # avoids both the old 512 KiB bitrate cap and multi-minute reads on a slow
    # candidate that should be rejected anyway.
    segment, _, seconds, total_size = fetch(segment_url, SEGMENT_SAMPLE_LIMIT)
    if len(segment) < 32 * 1024:
        raise ValueError("short_segment")
    download_mbps = len(segment) * 8 / seconds / 1_000_000
    stream_mbps = total_size * 8 / duration / 1_000_000 if duration else 0.0
    return download_mbps, stream_mbps, height


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
        return Probe(
            station,
            url,
            True,
            min(first[0], second[0]),
            min(first[1], second[1]),
            max(first[2], second[2]),
        )
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


def has_playback_headroom(probe: Probe) -> bool:
    # Judge delivery against the programme's real bitrate.  A fixed 5 Mbps
    # floor wrongly rejects a sustainable 1.7 Mbps stream downloading at
    # 3.5 Mbps, then favors a softer 1.3 Mbps picture only because its CDN can
    # burst at 15+ Mbps.  Two Mbps is merely a small absolute safety floor;
    # the 35% ratio is the actual anti-buffering requirement.
    return probe.mbps >= max(2.0, probe.stream_mbps * 1.35)


def balanced_probe_score(probe: Probe) -> float:
    """Prefer a clear stream with headroom, not the largest speed-test burst."""
    if not probe.ok:
        return -1e9
    if probe.height >= 1080:
        value = 145.0
    elif probe.height >= 720:
        value = 80.0
    elif probe.height >= 540:
        value = 25.0
    else:
        value = 45.0 if probe.stream_mbps >= 2.5 else 0.0
    value += min(probe.stream_mbps, 10.0) * 30.0
    # Once comfortable playback headroom is reached, additional burst speed
    # has little viewing value.  Cap and lightly weight it so programme
    # bitrate/resolution decide between two sustainable routes.
    value += min(probe.mbps, 8.0) * 0.6
    if probe.stream_mbps:
        headroom = probe.mbps / probe.stream_mbps
        if headroom >= 2.0:
            value += 55.0
        elif headroom >= 1.5:
            value += 42.0
        elif headroom >= 1.35:
            value += 28.0
        elif headroom >= 1.15:
            value -= 65.0
        else:
            value -= 260.0
        if probe.stream_mbps < 1.2:
            value -= 130.0
        elif probe.stream_mbps < 1.8:
            value -= 75.0
        elif probe.stream_mbps < 2.4:
            value -= 25.0
    return value


def is_viewer_confirmed_1080(probe: Probe) -> bool:
    if (
        not probe.ok
        or probe.url not in VIEWER_CONFIRMED_1080_ROUTES.get(probe.station, ())
        or probe.stream_mbps < VIEWER_CONFIRMED_MIN_STREAM_MBPS
    ):
        return False
    return probe.mbps >= max(
        2.0,
        probe.stream_mbps * VIEWER_CONFIRMED_MIN_HEADROOM,
    )


def probe_rank(probe: Probe, order: dict[str, int]) -> tuple:
    return (
        is_viewer_confirmed_1080(probe),
        has_playback_headroom(probe),
        balanced_probe_score(probe),
        -order.get(probe.url, len(order)),
    )


def choose_routes(results: list[Probe], previous: dict[str, str]) -> dict[str, Selection]:
    by_pair = {(probe.station, probe.url): probe for probe in results}
    healthy_by_station: dict[str, list[Probe]] = {}
    for station in ("cctv5", "cctv5plus"):
        order = {url: index for index, url in enumerate(CANDIDATES[station])}
        healthy = [
            probe for (probe_station, _), probe in by_pair.items()
            if probe_station == station and probe.ok
        ]
        healthy.sort(key=lambda probe: probe_rank(probe, order), reverse=True)
        healthy_by_station[station] = healthy

    # Optimize the two stations as a pair. Greedily giving the fastest host to
    # CCTV-5 can force CCTV-5+ onto a much lower-bitrate backup even though an
    # equally clear CCTV-5 route exists on a third host.
    if all(healthy_by_station.values()):
        pairs = [
            (cctv5, cctv5plus)
            for cctv5 in healthy_by_station["cctv5"]
            for cctv5plus in healthy_by_station["cctv5plus"]
            if cctv5.host != cctv5plus.host
        ]
        if not pairs:
            pairs = [
                (cctv5, cctv5plus)
                for cctv5 in healthy_by_station["cctv5"]
                for cctv5plus in healthy_by_station["cctv5plus"]
            ]
        cctv5, cctv5plus = max(
            pairs,
            key=lambda pair: (
                is_viewer_confirmed_1080(pair[0]),
                is_viewer_confirmed_1080(pair[1]),
                has_playback_headroom(pair[0]) and has_playback_headroom(pair[1]),
                int(has_playback_headroom(pair[0])) + int(has_playback_headroom(pair[1])),
                min(balanced_probe_score(pair[0]), balanced_probe_score(pair[1])),
                balanced_probe_score(pair[0]) + balanced_probe_score(pair[1]),
            ),
        )
        cctv5_reason = (
            "viewer_confirmed_1080"
            if is_viewer_confirmed_1080(cctv5)
            else "fresh_pair_probe"
        )
        return {
            "cctv5": Selection(cctv5.url, cctv5_reason, cctv5),
            "cctv5plus": Selection(cctv5plus.url, "fresh_pair_probe", cctv5plus),
        }

    chosen: dict[str, Selection] = {}
    used_hosts: set[str] = set()
    for station in ("cctv5", "cctv5plus"):
        healthy = healthy_by_station[station]
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
            "download_mbps": round(selected.probe.mbps, 2) if selected.probe else 0.0,
            "stream_mbps": round(selected.probe.stream_mbps, 2) if selected.probe else 0.0,
            "height": selected.probe.height if selected.probe else 0,
            "headroom": round(
                selected.probe.mbps / selected.probe.stream_mbps, 2
            ) if selected.probe and selected.probe.stream_mbps else 0.0,
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
    # Re-probe the routes just selected by the broad builder as well as the
    # curated rescue pool. Otherwise this final pass could overwrite a newly
    # discovered 4-6 Mbps picture with a faster but visibly softer 1 Mbps
    # fixed-pool route.
    previous: dict[str, str] = {}
    current_urls: dict[str, list[str]] = {"cctv5": [], "cctv5plus": []}
    for filename in PLAYLISTS:
        for station, url in existing_routes(Path(filename)).items():
            previous.setdefault(station, url)
            if url not in current_urls[station]:
                current_urls[station].append(url)

    jobs: list[tuple[str, str]] = []
    seen_jobs: set[tuple[str, str]] = set()
    for station, urls in CANDIDATES.items():
        for url in (*current_urls[station], *urls):
            marker = (station, url)
            if marker not in seen_jobs:
                jobs.append(marker)
                seen_jobs.add(marker)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = [executor.submit(probe_candidate, station, url) for station, url in jobs]
        results = [future.result() for future in futures]

    for result in results:
        state = "ok" if result.ok else f"fail:{result.error}"
        print(
            f"probe {result.station} {result.url} -> {state} "
            f"download={result.mbps:.2f} stream={result.stream_mbps:.2f} Mbps "
            f"height={result.height}"
        )

    selections = choose_routes(results, previous)
    for station, selected in selections.items():
        print(f"selected {station}={selected.url} reason={selected.reason}")

    for filename in PLAYLISTS:
        rewrite_playlist(Path(filename), selections)
    update_report(selections)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
