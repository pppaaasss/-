#!/usr/bin/env python3
"""Promote verified 1080p50 CCTV/mainland-satellite routes without duplicate tiles.

This pass is intentionally conservative. It mines public candidate lists, then
uses ffprobe plus a real HLS media-segment download to verify resolution, frame
rate, startup latency and throughput. A candidate only replaces the existing
URL when it is actually progressive ~50 fps at >=1080p with comfortable speed
headroom. Existing EXTINF metadata and visible channel names are preserved.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
REPORT = Path("fps-report.txt")
TIMEOUT = 8.0
FFPROBE_TIMEOUT = 13
MAX_PER_KEY = 10
WORKERS = 12
HOST_PROMOTION_CAP = 8
USER_AGENT = "Mozilla/5.0 (AppleTV; APTV 1080p50 verifier/1.0)"

SOURCES = (
    # Explicit 50FPS catalogue. Labels are only hints; every URL is re-probed.
    ("imdazui-50fps", "https://raw.githubusercontent.com/imDazui/Tvlist-awesome-m3u-m3u8/master/m3u/1300%E4%B8%AA%E7%9B%B4%E6%92%AD%E6%BA%90%E5%85%A8%E9%83%A8%E6%9C%89%E6%95%88%E3%80%90%E5%85%A8%E9%83%A84k%E8%80%81%E7%94%B5%E8%84%91%E5%88%AB%E7%94%A8%E3%80%91.m3u8"),
    # Fresh scanner and large maintained pools provide independent alternatives.
    ("vbsky-scan", "https://live.zbds.top/tv/iptv4.m3u"),
    ("iptv-org-cn", "https://iptv-org.github.io/iptv/countries/cn.m3u"),
    ("hujingguang-cn", "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV2_GuoNei.m3u8"),
    ("hujingguang-hunan", "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/HunanTV_AutoUpdate.m3u8"),
    ("xisohi-live", "https://raw.githubusercontent.com/xisohi/CHINA-IPTV/main/TV/live.txt"),
    ("datasource-live", "https://raw.githubusercontent.com/DataShare-duo/IPTV-Source/main/movie_live.txt"),
    # Legacy 50fps hints are never trusted by label; they must still pass today.
    ("legacy-50fps", "https://raw.githubusercontent.com/rekaabsex13/iptv/main/2022.3.m3u8"),
)

SATELLITES = (
    "北京卫视", "东方卫视", "湖南卫视", "浙江卫视", "江苏卫视", "广东卫视", "深圳卫视",
    "安徽卫视", "山东卫视", "河南卫视", "湖北卫视", "辽宁卫视", "黑龙江卫视", "四川卫视",
    "重庆卫视", "天津卫视", "河北卫视", "江西卫视", "广西卫视", "贵州卫视", "云南卫视",
    "陕西卫视", "山西卫视", "吉林卫视", "内蒙古卫视", "新疆卫视", "西藏卫视", "青海卫视",
    "甘肃卫视", "宁夏卫视", "海南卫视", "东南卫视", "延边卫视", "海峡卫视", "兵团卫视",
    "安多卫视", "农林卫视", "三沙卫视",
)
SAT_ALIASES = {"上海卫视": "东方卫视", "内蒙卫视": "内蒙古卫视", "旅游卫视": "海南卫视"}


@dataclass(frozen=True)
class Candidate:
    key: str
    name: str
    url: str
    source: str
    hint50: bool = False
    current: bool = False


@dataclass
class Result:
    candidate: Candidate
    ok: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    field_order: str = ""
    codec: str = ""
    bitrate_mbps: float = 0.0
    speed_mbps: float = 0.0
    stream_mbps: float = 0.0
    startup_s: float = 99.0
    error: str = ""

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.candidate.url).hostname or "").lower()

    @property
    def progressive50(self) -> bool:
        field = self.field_order.lower()
        progressive = field in {"", "unknown", "progressive"}
        return self.height >= 1080 and self.fps >= 49.0 and progressive

    @property
    def headroom_ok(self) -> bool:
        needed = max(6.0, self.stream_mbps * 1.35 if self.stream_mbps else 6.0)
        return self.speed_mbps >= needed and self.startup_s <= 4.0

    @property
    def promotable(self) -> bool:
        return self.ok and self.progressive50 and self.headroom_ok


def canonical_key(name: str, meta: str = "") -> str | None:
    text = f"{name} {meta}"
    low = text.lower()
    if re.search(r"cctv[\s_-]*4[\s_-]*k", low, re.I):
        return "cctv4k"
    if re.search(r"cctv[\s_-]*0?5\s*(?:\+|plus|p)", low, re.I):
        return "cctv5plus"
    match = re.search(r"cctv[\s_-]*0?(\d{1,2})(?!\d)", low, re.I)
    if match and 1 <= int(match.group(1)) <= 17:
        return f"cctv{int(match.group(1))}"
    # One old 50fps catalogue accidentally labels CCTV-9 as just "CCTV" but
    # retains the CCTV9 logo path. Recover only that narrow, unambiguous case.
    if "cctv9.png" in low and "50fps" in low:
        return "cctv9"
    for alias, canonical in SAT_ALIASES.items():
        if alias in text:
            return canonical
    for station in SATELLITES:
        if station in text:
            return station
    return None


def fetch_text(url: str, limit: int = 3_000_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read(limit).decode("utf-8", "ignore")


def parse_source(text: str, source: str) -> list[Candidate]:
    output: list[Candidate] = []
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
            name = extinf.rsplit(",", 1)[-1].strip()
            url = line.split("$", 1)[0].strip()
            key = canonical_key(name, extinf)
            if key and key != "cctv4k":
                output.append(Candidate(key, name, url, source, bool(re.search(r"50\s*fps|50p", f"{name} {extinf}", re.I))))
            extinf = None
            continue
        if "," in line:
            name, blob = (part.strip() for part in line.split(",", 1))
            key = canonical_key(name, line)
            if not key or key == "cctv4k" or "#genre#" in blob.lower():
                continue
            for route in blob.split("#"):
                hint = bool(re.search(r"\$\s*50\s*fps|50\s*fps|50p", route, re.I))
                url = route.split("$", 1)[0].strip()
                if url.startswith(("http://", "https://")):
                    output.append(Candidate(key, name, url, source, hint))
    return output


def playlist_entries(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    entries: list[tuple[str, str]] = []
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXTINF") and lines[i + 1].startswith(("http://", "https://")):
            entries.append((line, lines[i + 1]))
    return entries


def current_candidates() -> list[Candidate]:
    seen: set[tuple[str, str]] = set()
    output: list[Candidate] = []
    for playlist in PLAYLISTS:
        for extinf, url in playlist_entries(Path(playlist)):
            name = extinf.rsplit(",", 1)[-1].strip()
            key = canonical_key(name, extinf)
            if not key or key == "cctv4k" or (key, url) in seen:
                continue
            seen.add((key, url))
            output.append(Candidate(key, name, url, f"current:{playlist}", False, True))
    return output


def rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def ffprobe(url: str) -> tuple[int, int, float, str, str, float]:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe_not_found")
    cmd = [
        "ffprobe", "-v", "error", "-rw_timeout", "7000000",
        "-probesize", "2500000", "-analyzeduration", "3500000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,field_order,codec_name,bit_rate",
        "-show_entries", "format=bit_rate", "-of", "json", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe_failed")[-180:])
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("no_video_stream")
    stream = streams[0]
    fps = rate(stream.get("avg_frame_rate")) or rate(stream.get("r_frame_rate"))
    bitrate = 0.0
    for raw in (stream.get("bit_rate"), (payload.get("format") or {}).get("bit_rate")):
        try:
            bitrate = max(bitrate, float(raw or 0) / 1_000_000)
        except (TypeError, ValueError):
            pass
    return (
        int(stream.get("width") or 0), int(stream.get("height") or 0), fps,
        str(stream.get("field_order") or ""), str(stream.get("codec_name") or ""), bitrate,
    )


def read_url(url: str, limit: int, timeout: float = TIMEOUT) -> tuple[bytes, float, str, int]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Range": f"bytes=0-{limit - 1}"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        final_url = response.geturl()
        data = response.read(limit)
        total = 0
        cr = response.headers.get("Content-Range", "")
        match = re.search(r"/([0-9]+)$", cr)
        if match:
            total = int(match.group(1))
        elif response.headers.get("Content-Length"):
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
        if not total and len(data) < limit:
            total = len(data)
    return data, max(time.monotonic() - started, 0.001), final_url, total


def media_playlist(text: str, base: str) -> tuple[str, str, float]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[tuple[int, float, str]] = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        resolution = re.search(r"RESOLUTION=\d+x(\d+)", line, re.I)
        frame = re.search(r"FRAME-RATE=([0-9.]+)", line, re.I)
        height = int(resolution.group(1)) if resolution else 0
        fps = float(frame.group(1)) if frame else 0.0
        for nxt in lines[i + 1:]:
            if nxt.startswith("#"):
                continue
            variants.append((height, fps, urllib.parse.urljoin(base, nxt)))
            break
    if variants:
        normal = [row for row in variants if 720 <= row[0] <= 1080]
        chosen = max(normal or variants, key=lambda row: (row[0] <= 1080, row[0], row[1]))
        data, _, final, _ = read_url(chosen[2], 512 * 1024)
        return data.decode("utf-8", "ignore"), final, chosen[1]
    return text, base, 0.0


def hls_speed(url: str) -> tuple[float, float, float, float]:
    manifest, manifest_s, final, _ = read_url(url, 512 * 1024)
    text = manifest.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise RuntimeError("not_hls")
    media, media_url, manifest_fps = media_playlist(text, final)
    duration = 0.0
    segment_url = ""
    for line in media.splitlines():
        item = line.strip()
        if item.upper().startswith("#EXTINF:"):
            match = re.match(r"#EXTINF:([0-9.]+)", item, re.I)
            duration = float(match.group(1)) if match else 0.0
            continue
        if item and not item.startswith("#"):
            segment_url = urllib.parse.urljoin(media_url, item)
            break
    if not segment_url:
        raise RuntimeError("no_segment")
    data, seconds, _, total = read_url(segment_url, 1024 * 1024)
    if len(data) < 32 * 1024:
        raise RuntimeError("short_segment")
    speed = len(data) * 8 / seconds / 1_000_000
    stream = total * 8 / duration / 1_000_000 if total and duration > 0 else 0.0
    return speed, stream, manifest_s, manifest_fps


def evaluate(candidate: Candidate) -> Result:
    result = Result(candidate)
    try:
        width, height, fps, field_order, codec, bitrate = ffprobe(candidate.url)
        speed, stream, startup, manifest_fps = hls_speed(candidate.url)
        # HLS FRAME-RATE is useful when ffprobe reports a zero rate, but never
        # overrides a nonzero decoded stream rate.
        if fps <= 0 and manifest_fps > 0:
            fps = manifest_fps
        result.ok = True
        result.width = width
        result.height = height
        result.fps = fps
        result.field_order = field_order
        result.codec = codec
        result.bitrate_mbps = bitrate
        result.speed_mbps = speed
        result.stream_mbps = stream or bitrate
        result.startup_s = startup
    except Exception as exc:
        result.error = f"{type(exc).__name__}:{str(exc)[:160]}"
    return result


def prelim_rank(candidate: Candidate) -> tuple[int, int, int]:
    label = candidate.name.lower()
    return (
        1 if candidate.current else 0,
        1 if candidate.hint50 or "50fps" in label or "50p" in label else 0,
        1 if re.search(r"1080|fhd|高清|hd", label, re.I) else 0,
    )


def score(result: Result) -> float:
    if not result.ok:
        return -1e9
    value = 0.0
    if result.progressive50:
        value += 1400
    if result.height >= 1080:
        value += 500
    elif result.height >= 720:
        value += 100
    value += min(result.speed_mbps, 40) * 6
    value += min(result.stream_mbps, 15) * 18
    value -= result.startup_s * 28
    if result.candidate.hint50:
        value += 20  # tiny hint only; decoded fps dominates.
    if result.candidate.current:
        value += 10
    return value


def replace_urls(path: Path, chosen: dict[str, Result]) -> int:
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    replaced = 0
    i = 0
    while i + 1 < len(lines):
        if lines[i].startswith("#EXTINF") and lines[i + 1].startswith(("http://", "https://")):
            name = lines[i].rsplit(",", 1)[-1].strip()
            key = canonical_key(name, lines[i])
            result = chosen.get(key or "")
            if result and lines[i + 1] != result.candidate.url:
                lines[i + 1] = result.candidate.url
                replaced += 1
            i += 2
            continue
        i += 1
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    return replaced


def main() -> int:
    if not shutil.which("ffprobe"):
        print("ffprobe missing; leaving playlists unchanged")
        return 0

    candidates = current_candidates()
    source_status: list[str] = []
    for source, url in SOURCES:
        try:
            parsed = parse_source(fetch_text(url), source)
            candidates.extend(parsed)
            source_status.append(f"{source}:ok:{len(parsed)}")
        except Exception as exc:
            source_status.append(f"{source}:fail:{type(exc).__name__}")

    unique: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        marker = (candidate.key, candidate.url)
        old = unique.get(marker)
        if old is None or prelim_rank(candidate) > prelim_rank(old):
            unique[marker] = candidate

    by_key: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in unique.values():
        by_key[candidate.key].append(candidate)

    probe_list: list[Candidate] = []
    for key, items in by_key.items():
        items.sort(key=prelim_rank, reverse=True)
        current = [item for item in items if item.current]
        labelled = [item for item in items if item.hint50 or re.search(r"50\s*fps|50p", item.name, re.I)]
        ordinary = [item for item in items if item not in current and item not in labelled]
        selected: list[Candidate] = []
        seen_hosts: Counter[str] = Counter()
        for item in current + labelled + ordinary:
            host = (urllib.parse.urlsplit(item.url).hostname or "").lower()
            # Keep multiple 50fps mirrors, but prefer host diversity in the
            # expensive ffprobe pass.
            if item not in current and seen_hosts[host] >= 2:
                continue
            selected.append(item)
            seen_hosts[host] += 1
            if len(selected) >= MAX_PER_KEY:
                break
        probe_list.extend(selected)

    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {executor.submit(evaluate, candidate): candidate for candidate in probe_list}
        for future in concurrent.futures.as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(Result(future_map[future], error=f"executor:{type(exc).__name__}"))

    result_by_key: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        result_by_key[result.candidate.key].append(result)
    for items in result_by_key.values():
        items.sort(key=score, reverse=True)

    host_promotions: Counter[str] = Counter()
    chosen: dict[str, Result] = {}
    for key in sorted(result_by_key):
        current_results = [r for r in result_by_key[key] if r.candidate.current and r.ok]
        current_best = max(current_results, key=score, default=None)
        for result in result_by_key[key]:
            if not result.promotable:
                continue
            if result.candidate.current:
                break
            if host_promotions[result.host] >= HOST_PROMOTION_CAP:
                continue
            # Verify the winning 50p route one more time before publication.
            try:
                speed2, stream2, startup2, _ = hls_speed(result.candidate.url)
            except Exception:
                continue
            need2 = max(5.0, (stream2 or result.stream_mbps) * 1.25)
            if speed2 < need2 or startup2 > 4.5:
                continue
            result.speed_mbps = min(result.speed_mbps, speed2)
            if stream2:
                result.stream_mbps = min(x for x in (result.stream_mbps, stream2) if x > 0) if result.stream_mbps else stream2
            result.startup_s = max(result.startup_s, startup2)
            if not result.headroom_ok:
                continue
            # A verified 1080p50 route can replace 25p/unknown. If the current
            # route is already verified 1080p50, only switch for a clear score win.
            if current_best and current_best.progressive50 and score(result) < score(current_best) + 80:
                break
            chosen[key] = result
            host_promotions[result.host] += 1
            break

    replacements = {name: replace_urls(Path(name), chosen) for name in PLAYLISTS}
    verified = [r for r in results if r.promotable]
    lines = [
        f"generated_utc={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        "source_status=" + json.dumps(source_status, ensure_ascii=False),
        f"candidate_routes={len(unique)}",
        f"probed_routes={len(results)}",
        f"probe_ok={sum(r.ok for r in results)}",
        f"verified_1080p50={len(verified)}",
        f"promoted_stations={len(chosen)}",
        "promotion_hosts=" + json.dumps(dict(host_promotions), ensure_ascii=False, sort_keys=True),
        "playlist_replacements=" + json.dumps(replacements, ensure_ascii=False, sort_keys=True),
        "promotions=" + json.dumps([
            {
                "key": key,
                "url": result.candidate.url,
                "source": result.candidate.source,
                "host": result.host,
                "width": result.width,
                "height": result.height,
                "fps": round(result.fps, 3),
                "field_order": result.field_order,
                "codec": result.codec,
                "download_mbps": round(result.speed_mbps, 2),
                "stream_mbps": round(result.stream_mbps, 2),
                "startup_s": round(result.startup_s, 3),
            }
            for key, result in sorted(chosen.items())
        ], ensure_ascii=False),
        "top_1080p50=" + json.dumps([
            {
                "key": r.candidate.key,
                "source": r.candidate.source,
                "host": r.host,
                "fps": round(r.fps, 3),
                "height": r.height,
                "download_mbps": round(r.speed_mbps, 2),
                "stream_mbps": round(r.stream_mbps, 2),
                "startup_s": round(r.startup_s, 3),
                "url": r.candidate.url,
            }
            for r in sorted(verified, key=score, reverse=True)[:80]
        ], ensure_ascii=False),
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:9]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
