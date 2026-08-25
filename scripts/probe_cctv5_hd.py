#!/usr/bin/env python3
"""Fast CCTV-5 quality probe for public candidates.

This is intentionally separate from the broad playlist builder. It measures a
small set of CCTV-5 routes with ffprobe plus real HLS segment downloads and
writes cctv5-probe-report.txt. It never edits the published playlists.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (AppleTV; APTV CCTV5 HD probe/1.0)"
HTTP_TIMEOUT = 7.0
FFPROBE_TIMEOUT = 14
READ_LIMIT = 4 * 1024 * 1024
WORKERS = 8
REPORT = Path("cctv5-probe-report.txt")

# Regional pools already confirmed unusable on the viewer's home line are
# intentionally absent. Candidates below favour public/recently-maintained
# routes and independent hosts. A label is only a hint; measured metadata wins.
CANDIDATES = (
    ("public-hd-a", "http://198.204.228.26/live/cctv5hd.m3u8"),
    ("public-hd-b", "http://107.150.60.122/live/cctv5hd.m3u8"),
    ("public-hd-c", "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub"),
    ("public-hd-d", "http://207.56.13.146:81/cdnlive/cctv5.m3u8"),
    ("public-1080", "http://173.208.212.130:8181/1080p/cctv5.m3u8"),
    ("aliyun-public", "http://120.76.248.139/live/bfgd/4200000064.m3u8"),
    ("home-gateway-a", "http://111.4.59.41:60901/tsfile/live/1004_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("home-gateway-b", "http://123.118.55.165:45237/tsfile/live/0005_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("home-gateway-c", "http://182.114.49.102:9901/tsfile/live/0005_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("public-hls-a", "http://113.57.140.161:10081/newlive/live/hls/5/live.m3u8"),
    ("public-hls-b", "http://1.24.39.180:9003/hls/5/index.m3u8"),
    ("current-family", "http://221.7.175.154:8445/tsfile/live/1018_1.m3u8?key=txiptv&playlive=1&authid=0"),
)


@dataclass
class Result:
    label: str
    url: str
    ok: bool = False
    width: int = 0
    height: int = 0
    avg_fps: float = 0.0
    r_fps: float = 0.0
    field_order: str = ""
    codec: str = ""
    ffprobe_mbps: float = 0.0
    stream_mbps: float = 0.0
    download_mbps: float = 0.0
    startup_s: float = 99.0
    second_download_mbps: float = 0.0
    mode: str = "unknown"
    error: str = ""

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").lower()

    @property
    def score(self) -> float:
        if not self.ok:
            return -1e9
        mode_bonus = {
            "1080p50": 3000,
            "1080i50": 2700,
            "1080p25": 1800,
            "1080-other": 1500,
            "720p50": 1000,
            "720-other": 600,
        }.get(self.mode, 0)
        speed = min(self.download_mbps, self.second_download_mbps or self.download_mbps)
        return mode_bonus + min(speed, 50) * 8 + min(self.stream_mbps, 20) * 20 - self.startup_s * 50


def rate(value: str | None) -> float:
    if not value or value in {"N/A", "0/0"}:
        return 0.0
    try:
        if "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def fetch(url: str, limit: int = READ_LIMIT) -> tuple[bytes, str, float, int]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.apple.mpegurl,*/*",
            "Range": f"bytes=0-{limit - 1}",
        },
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        data = response.read(limit)
        elapsed = max(time.monotonic() - started, 0.001)
        total = 0
        content_range = response.headers.get("Content-Range", "")
        match = re.search(r"/(\d+)$", content_range)
        if match:
            total = int(match.group(1))
        elif response.headers.get("Content-Length"):
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
        if not total and len(data) < limit:
            total = len(data)
        return data, response.geturl(), elapsed, total


def choose_media(text: str, base_url: str) -> tuple[str, str, float]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[tuple[int, int, float, str]] = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
        frame = re.search(r"FRAME-RATE=([0-9.]+)", line, re.I)
        height = int(resolution.group(2)) if resolution else 0
        bw = int(bandwidth.group(1)) if bandwidth else 0
        fps = float(frame.group(1)) if frame else 0.0
        for nxt in lines[index + 1 :]:
            if not nxt or nxt.startswith("#"):
                continue
            variants.append((height, bw, fps, urllib.parse.urljoin(base_url, nxt)))
            break
    if not variants:
        return text, base_url, 0.0
    normal = [row for row in variants if 720 <= row[0] <= 1080]
    chosen = max(normal or variants, key=lambda row: (row[0], row[1], row[2]))
    data, final, _, _ = fetch(chosen[3], 512 * 1024)
    media = data.decode("utf-8", "ignore")
    if "#EXTM3U" not in media:
        raise RuntimeError("bad_variant")
    return media, final, chosen[2]


def newest_segment(media: str, base_url: str) -> tuple[str, float]:
    pending_duration = 0.0
    pairs: list[tuple[str, float]] = []
    for raw in media.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF:"):
            match = re.match(r"#EXTINF:([0-9.]+)", line, re.I)
            pending_duration = float(match.group(1)) if match else 0.0
        elif line and not line.startswith("#"):
            pairs.append((urllib.parse.urljoin(base_url, line), pending_duration))
            pending_duration = 0.0
    if not pairs:
        raise RuntimeError("no_segment")
    return pairs[-1]


def hls_measure(url: str) -> tuple[float, float, float, float]:
    manifest, final, manifest_s, _ = fetch(url, 512 * 1024)
    text = manifest.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise RuntimeError("not_hls")
    media, media_url, manifest_fps = choose_media(text, final)
    upper = media.upper().replace(" ", "")
    if "#EXT-X-ENDLIST" in upper or "#EXT-X-PLAYLIST-TYPE:VOD" in upper:
        raise RuntimeError("vod_playlist")
    segment_url, duration = newest_segment(media, media_url)
    data, _, seconds, total = fetch(segment_url)
    if len(data) < 32 * 1024:
        raise RuntimeError("short_segment")
    download = len(data) * 8 / seconds / 1_000_000
    segment_bytes = total or len(data)
    stream = segment_bytes * 8 / duration / 1_000_000 if duration > 0 else 0.0
    return download, stream, manifest_s, manifest_fps


def ffprobe_meta(url: str) -> tuple[int, int, float, float, str, str, float]:
    cmd = [
        "ffprobe", "-v", "error", "-rw_timeout", "7000000",
        "-probesize", "3000000", "-analyzeduration", "5000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,field_order,codec_name,bit_rate",
        "-show_entries", "format=bit_rate", "-of", "json", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe_failed")[-200:])
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("no_video_stream")
    stream = streams[0]
    bitrate = 0.0
    for raw in (stream.get("bit_rate"), (payload.get("format") or {}).get("bit_rate")):
        try:
            bitrate = max(bitrate, float(raw or 0) / 1_000_000)
        except (TypeError, ValueError):
            pass
    return (
        int(stream.get("width") or 0),
        int(stream.get("height") or 0),
        rate(stream.get("avg_frame_rate")),
        rate(stream.get("r_frame_rate")),
        str(stream.get("field_order") or ""),
        str(stream.get("codec_name") or ""),
        bitrate,
    )


def classify(height: int, avg_fps: float, r_fps: float, field: str) -> str:
    fps = max(avg_fps, r_fps)
    progressive = field.lower() in {"", "unknown", "progressive"}
    if height >= 1080 and progressive and fps >= 49:
        return "1080p50"
    if height >= 1080 and not progressive and r_fps >= 49 and avg_fps >= 24:
        return "1080i50"
    if height >= 1080 and progressive and 24 <= fps < 49:
        return "1080p25"
    if height >= 1080:
        return "1080-other"
    if height >= 720 and fps >= 49:
        return "720p50"
    if height >= 720:
        return "720-other"
    return "sd"


def probe(label: str, url: str) -> Result:
    result = Result(label, url)
    try:
        width, height, avg_fps, r_fps, field, codec, bitrate = ffprobe_meta(url)
        speed1, stream1, startup1, manifest_fps = hls_measure(url)
        time.sleep(0.35)
        speed2, stream2, startup2, manifest_fps2 = hls_measure(url)
        if not avg_fps:
            avg_fps = manifest_fps or manifest_fps2
        if not r_fps:
            r_fps = manifest_fps or manifest_fps2
        result.ok = True
        result.width = width
        result.height = height
        result.avg_fps = avg_fps
        result.r_fps = r_fps
        result.field_order = field
        result.codec = codec
        result.ffprobe_mbps = bitrate
        result.stream_mbps = min(v for v in (stream1 or bitrate, stream2 or bitrate) if v > 0) if any(v > 0 for v in (stream1, stream2, bitrate)) else 0.0
        result.download_mbps = speed1
        result.second_download_mbps = speed2
        result.startup_s = max(startup1, startup2)
        result.mode = classify(height, avg_fps, r_fps, field)
    except Exception as exc:
        result.error = f"{type(exc).__name__}:{str(exc)[:180]}"
    return result


def main() -> int:
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        results = list(executor.map(lambda item: probe(*item), CANDIDATES))

    ranked = sorted(results, key=lambda item: item.score, reverse=True)
    lines = [
        f"generated_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"candidates={len(results)}",
        f"reachable={sum(r.ok for r in results)}",
        f"1080p50={sum(r.ok and r.mode == '1080p50' for r in results)}",
        f"1080i50={sum(r.ok and r.mode == '1080i50' for r in results)}",
        "ranking=" + json.dumps([
            {
                **asdict(r),
                "host": r.host,
                "score": round(r.score, 2),
                "headroom": round(min(r.download_mbps, r.second_download_mbps or r.download_mbps) / r.stream_mbps, 2) if r.stream_mbps else 0.0,
            }
            for r in ranked
        ], ensure_ascii=False),
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for r in ranked:
        state = r.mode if r.ok else f"FAIL {r.error}"
        print(
            f"{r.label:16} {state:18} {r.width}x{r.height} "
            f"avg={r.avg_fps:.2f} r={r.r_fps:.2f} field={r.field_order or '-':10} "
            f"stream={r.stream_mbps:.2f}Mbps dl={r.download_mbps:.2f}/{r.second_download_mbps:.2f}Mbps "
            f"start={r.startup_s:.2f}s {r.url}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
