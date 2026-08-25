#!/usr/bin/env python3
"""Keep CCTV-5 usable even when the GitHub runner and the viewer see different routes.

The main builder already performs broad health checks, but a route can pass from a
GitHub Actions runner and still stall on the viewer's ISP/VPN path. This final
pass publishes several independent CCTV-5 hosts as visible APTV tiles instead of
betting the whole channel on one remote probe result.
"""

from __future__ import annotations

import concurrent.futures
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV CCTV5 rescue/1.0)"
TIMEOUT = 7.0
READ_LIMIT = 512 * 1024
MAIN_BACKUP_COUNT = 3

# Fresh public 1080p candidates reviewed in August 2026. The first two come
# from a list that keeps separate CCTV-5 and CCTV-5+ main/backup routes; the
# rest deliberately span unrelated hosts so one provider cannot take all
# visible escape routes down at once. Never add cctv5p/cctv5plus or /audio/.
CANDIDATES = (
    "http://198.204.228.26/live/cctv5hd.m3u8",
    "http://183.223.157.33:9901/tsfile/live/0005_1.m3u8?key=txiptv&playlive=1&authid=0",
    "http://112.27.235.94:8000/hls/5/index.m3u8",
    "http://107.150.60.122/live/cctv5hd.m3u8",
    "http://74.91.26.218:82/live/cctv5hd.m3u8",
    "http://69.30.245.50/live/cctv5.m3u8",
    "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub",
    "http://207.56.13.146:81/cdnlive/cctv5.m3u8",
)


@dataclass(frozen=True)
class Probe:
    url: str
    ok: bool
    mbps: float = 0.0
    error: str = ""


def fetch(url: str, limit: int = READ_LIMIT) -> tuple[bytes, str, float]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept": "application/vnd.apple.mpegurl,*/*",
            "Range": f"bytes=0-{limit - 1}",
        },
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
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
            if not candidate or candidate.startswith("#"):
                continue
            rows.append((height, rate, urllib.parse.urljoin(base_url, candidate)))
            break
    if not rows:
        return None
    normal = [row for row in rows if 720 <= row[0] <= 1080]
    chosen = max(normal or rows, key=lambda row: (row[0], row[1]))
    return chosen[2]


def first_media_uri(text: str, base_url: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return urllib.parse.urljoin(base_url, line)
    return None


def probe_once(url: str) -> float:
    manifest, final_url, _ = fetch(url)
    text = manifest.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise ValueError("not_hls")

    variant = choose_variant(text, final_url)
    if variant:
        media, media_url, _ = fetch(variant)
        text = media.decode("utf-8", "ignore")
        final_url = media_url
        if "#EXTM3U" not in text:
            raise ValueError("bad_variant")

    upper = text.upper()
    if "#EXT-X-ENDLIST" in upper or "#EXT-X-PLAYLIST-TYPE:VOD" in upper.replace(" ", ""):
        raise ValueError("vod_playlist")
    segment_url = first_media_uri(text, final_url)
    if not segment_url:
        raise ValueError("no_segment")
    segment, _, seconds = fetch(segment_url)
    if len(segment) < 32 * 1024:
        raise ValueError("short_segment")
    return len(segment) * 8 / seconds / 1_000_000


def probe(url: str) -> Probe:
    path = urllib.parse.urlsplit(url).path.lower()
    if "/audio/" in path or re.search(r"cctv5(?:p|plus)", path, re.I):
        return Probe(url, False, error="wrong_cctv5_variant")
    try:
        first = probe_once(url)
        second = probe_once(url)
        return Probe(url, True, min(first, second))
    except Exception as exc:  # Network failures are expected for regional routes.
        return Probe(url, False, error=f"{type(exc).__name__}:{str(exc)[:80]}")


def display_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def is_cctv5_tile(extinf: str) -> bool:
    name = display_name(extinf).lower().replace("－", "-")
    if "+" in name or "plus" in name:
        return False
    return bool(re.fullmatch(r"cctv\s*-?\s*5(?:\s+备用\d+(?:\s+1080p)?)?", name, re.I))


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


def with_name(extinf: str, name: str) -> str:
    return extinf.rsplit(",", 1)[0] + "," + name if "," in extinf else extinf + "," + name


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


def rewrite_playlist(path: Path, urls: list[str], backup_count: int) -> bool:
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8", errors="ignore")
    header, entries = parse_playlist(original)
    cctv5_indexes = [i for i, (extinf, _) in enumerate(entries) if is_cctv5_tile(extinf)]
    if not cctv5_indexes:
        print(f"{path}: no CCTV-5 tile found; left untouched")
        return False

    first_index = cctv5_indexes[0]
    template = entries[first_index][0]
    cctv5_index_set = set(cctv5_indexes)
    cleaned = [entry for i, entry in enumerate(entries) if i not in cctv5_index_set]
    published = urls[: 1 + backup_count]
    replacements: list[tuple[str, str]] = []
    for index, url in enumerate(published):
        name = "CCTV-5" if index == 0 else f"CCTV-5 备用{index} 1080p"
        replacements.append((with_name(template, name), url))
    cleaned[first_index:first_index] = replacements
    header = update_count_header(header, len(cleaned))
    rendered = "\n".join(header + [part for entry in cleaned for part in entry]) + "\n"
    if rendered == original:
        return False
    path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"{path}: published {len(replacements)} CCTV-5 routes")
    return True


def main() -> int:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CANDIDATES)) as executor:
        results = list(executor.map(probe, CANDIDATES))

    by_url = {result.url: result for result in results}
    # Preserve research order first; it reflects freshness and host diversity.
    # Passing routes are moved ahead, but a China-only route that the overseas
    # runner cannot reach is still retained as a visible manual escape route.
    ranked = sorted(
        CANDIDATES,
        key=lambda url: (
            not by_url[url].ok,
            CANDIDATES.index(url),
            -by_url[url].mbps,
        ),
    )

    selected: list[str] = []
    used_hosts: set[str] = set()
    for url in ranked:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host or host in used_hosts:
            continue
        selected.append(url)
        used_hosts.add(host)
        if len(selected) >= 1 + MAIN_BACKUP_COUNT:
            break

    if not selected:
        print("No CCTV-5 rescue candidates available; playlists left untouched")
        return 0

    for result in results:
        state = "ok" if result.ok else f"fail:{result.error}"
        print(f"probe {result.url} -> {state} {result.mbps:.2f} Mbps")
    print("selected=" + " | ".join(selected))

    rewrite_playlist(Path("tv.m3u"), selected, MAIN_BACKUP_COUNT)
    rewrite_playlist(Path("tv-all.m3u"), selected, MAIN_BACKUP_COUNT)
    rewrite_playlist(Path("tv-easy.m3u"), selected, 0)

    report = Path("build-report.txt")
    if report.exists():
        import json

        text = report.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"^cctv5_rescue_routes=.*\n?", "", text, flags=re.M)
        status = [
            {
                "url": url,
                "runner_ok": by_url[url].ok,
                "runner_mbps": round(by_url[url].mbps, 2),
            }
            for url in selected
        ]
        report.write_text(
            text.rstrip("\n") + "\ncctv5_rescue_routes=" + json.dumps(status, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
