#!/usr/bin/env python3
"""Read-only ffprobe audit for the published APTV playlists.

The auditor never rewrites a playlist. Resolution/codec/frame-rate/interlace are
obtained from ffprobe decoding the selected video input. HLS RESOLUTION labels
are used only to enumerate variants, never as proof of picture quality.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (AppleTV; APTV production quality audit/3.0)"
CCTV_RE = re.compile(r"^CCTV-(?:[1-9]|1[0-7])$")
SHORT_TOKEN_RE = re.compile(r"(?:^|[?&])(exp|expires|expire|expiry)=([0-9]{9,13})(?:&|$)", re.I)
PRODUCTION_FILES = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")


@dataclass(frozen=True)
class Entry:
    index: int
    name: str
    group: str
    url: str


def parse_playlist(path: Path) -> list[Entry]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: list[Entry] = []
    pending: tuple[str, str] | None = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            match = re.search(r'group-title="([^"]*)"', line, re.I)
            pending = (name, match.group(1).strip() if match else "")
            continue
        if pending and line.startswith(("http://", "https://")):
            out.append(Entry(len(out) + 1, pending[0], pending[1], line))
            pending = None
        elif line and not line.startswith("#"):
            pending = None
    return out


def is_core(name: str, group: str) -> bool:
    if CCTV_RE.fullmatch(name) or name in {"CCTV-5+", "CCTV-4K"}:
        return True
    if group == "卫视台" and name.endswith("卫视") and name not in {"凤凰卫视", "澳亚卫视"}:
        return True
    return False


def request_bytes(url: str, *, timeout: float, limit: int = 1_500_000) -> tuple[bytes, str, float, float]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.apple.mpegurl,*/*"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        opened = time.monotonic()
        data = response.read(limit)
        finished = time.monotonic()
        return data, response.geturl(), max(opened - started, 0.001), max(finished - started, 0.001)


def parse_variants(text: str, base_url: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    out: list[str] = []
    for i, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        for nxt in lines[i + 1 :]:
            if nxt and not nxt.startswith("#"):
                url = urllib.parse.urljoin(base_url, nxt)
                if url not in out:
                    out.append(url)
                break
    return out


def parse_fraction(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        if "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def ffprobe(url: str, timeout: float) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error",
        "-rw_timeout", str(int(timeout * 1_000_000)),
        "-user_agent", UA,
        "-analyzeduration", "7000000", "-probesize", "7000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,field_order,bit_rate:format=bit_rate",
        "-of", "json", url,
    ]
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 4, check=False)
    elapsed = time.monotonic() - started
    if completed.returncode:
        raise RuntimeError((completed.stderr or "ffprobe failed").strip()[-500:])
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("ffprobe returned no video stream")
    stream = streams[0]
    fmt = payload.get("format") or {}
    bit_rate = int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)
    field = str(stream.get("field_order") or "unknown").lower()
    if field in {"tt", "bb", "tb", "bt"}:
        scan = "interlaced"
    elif field == "progressive":
        scan = "progressive"
    else:
        scan = "unknown"
    fps = parse_fraction(stream.get("avg_frame_rate")) or parse_fraction(stream.get("r_frame_rate"))
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": str(stream.get("codec_name") or "unknown"),
        "fps": round(fps, 3),
        "scan": scan,
        "field_order": field,
        "stream_mbps": round(bit_rate / 1_000_000, 3) if bit_rate else 0.0,
        "probe_seconds": round(elapsed, 3),
    }


def media_segments(text: str, base_url: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF:"):
            match = re.search(r"#EXTINF:([0-9.]+)", line, re.I)
            duration = float(match.group(1)) if match else 0.0
        elif line and not line.startswith("#"):
            out.append((urllib.parse.urljoin(base_url, line), duration))
            duration = 0.0
    return out


def measure_url(url: str, timeout: float, limit: int = 8_000_000) -> tuple[float, float, int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        first = response.read(64 * 1024)
        first_at = time.monotonic()
        total = len(first)
        while total < limit:
            chunk = response.read(min(256 * 1024, limit - total))
            if not chunk:
                break
            total += len(chunk)
        finished = time.monotonic()
        seconds = max(finished - started, 0.001)
        mbps = total * 8 / seconds / 1_000_000
        return round(mbps, 3), round(max(first_at - started, 0.001), 3), total, response.geturl()


def estimate_bitrate_and_speed(media_url: str, timeout: float, samples: int) -> tuple[float, list[float], list[float], str]:
    try:
        manifest, final_url, _, _ = request_bytes(media_url, timeout=timeout)
        text = manifest.decode("utf-8", "ignore")
    except Exception:
        return 0.0, [], [], media_url
    if "#EXTM3U" not in text:
        speeds: list[float] = []
        starts: list[float] = []
        final = final_url
        for _ in range(samples):
            try:
                speed, start, _, final = measure_url(final_url, timeout)
                speeds.append(speed)
                starts.append(start)
            except Exception:
                pass
        return 0.0, speeds, starts, final

    segs = media_segments(text, final_url)
    if not segs:
        return 0.0, [], [], final_url
    chosen = segs[-max(samples, 1):]
    speeds: list[float] = []
    starts: list[float] = []
    bitrate_samples: list[float] = []
    final = final_url
    for seg_url, duration in chosen:
        try:
            speed, start, total, final = measure_url(seg_url, timeout)
            speeds.append(speed)
            starts.append(start)
            if duration > 0 and total > 0:
                bitrate_samples.append(total * 8 / duration / 1_000_000)
        except Exception:
            pass
    est = sum(bitrate_samples) / len(bitrate_samples) if bitrate_samples else 0.0
    return round(est, 3), speeds, starts, final


def token_expiry(url: str) -> tuple[bool, int | None]:
    match = SHORT_TOKEN_RE.search(url)
    if not match:
        return False, None
    raw = int(match.group(2))
    expiry = raw // 1000 if raw > 10_000_000_000 else raw
    remaining = expiry - int(time.time())
    return remaining < 24 * 3600, remaining


def resolution_bucket(height: int) -> str:
    if height >= 2160:
        return "2160_plus"
    if height >= 1080:
        return "1080"
    if height >= 720:
        return "720"
    if height > 0:
        return "below_720"
    return "unknown"


def audit_entry(entry: Entry, timeout: float, samples: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "index": entry.index, "name": entry.name, "group": entry.group, "url": entry.url,
        "final_url": "", "is_core": is_core(entry.name, entry.group), "is_master_playlist": False,
        "tested_variant_url": "", "variant_actual_heights": [], "has_480_540_576_720_variant": False,
        "width": 0, "height": 0, "codec": "unknown", "fps": 0.0, "scan": "unknown",
        "field_order": "unknown", "stream_mbps": 0.0, "download_mbps_samples": [],
        "worst_download_mbps": 0.0, "startup_seconds_samples": [], "worst_startup_seconds": 0.0,
        "headroom": 0.0, "short_lived_token": False, "token_remaining_seconds": None,
        "status": "unknown", "quality_gate": "unknown", "final_version_accepted": False, "error": "",
    }
    try:
        manifest, final_url, _, _ = request_bytes(entry.url, timeout=timeout)
        row["final_url"] = final_url
        text = manifest.decode("utf-8", "ignore")
        variants = parse_variants(text, final_url) if "#EXTM3U" in text else []
        row["is_master_playlist"] = bool(variants)
        actual_variants: list[tuple[str, dict[str, Any]]] = []
        if variants:
            for variant_url in variants:
                try:
                    actual = ffprobe(variant_url, timeout)
                    actual_variants.append((variant_url, actual))
                except Exception:
                    continue
            if not actual_variants:
                raise RuntimeError("master playlist has no decodable variants")
            actual_variants.sort(key=lambda item: (item[1]["height"], item[1]["stream_mbps"], item[1]["width"]), reverse=True)
            tested_url, actual = actual_variants[0]
            row["tested_variant_url"] = tested_url
            heights = sorted({int(info["height"]) for _, info in actual_variants if int(info["height"]) > 0})
            row["variant_actual_heights"] = heights
            row["has_480_540_576_720_variant"] = any(h in {480, 540, 576, 720} for h in heights)
        else:
            tested_url = final_url or entry.url
            row["tested_variant_url"] = tested_url
            actual = ffprobe(tested_url, timeout)
        for key in ("width", "height", "codec", "fps", "scan", "field_order", "stream_mbps"):
            row[key] = actual[key]
        estimated, speeds, starts, _ = estimate_bitrate_and_speed(tested_url, timeout, samples)
        if not row["stream_mbps"] and estimated:
            row["stream_mbps"] = estimated
        row["download_mbps_samples"] = speeds
        row["startup_seconds_samples"] = starts
        row["worst_download_mbps"] = round(min(speeds), 3) if speeds else 0.0
        row["worst_startup_seconds"] = round(max(starts), 3) if starts else 0.0
        if row["stream_mbps"] and row["worst_download_mbps"]:
            row["headroom"] = round(row["worst_download_mbps"] / row["stream_mbps"], 3)
        short, remaining = token_expiry(tested_url)
        row["short_lived_token"] = short
        row["token_remaining_seconds"] = remaining
        suspected_identity = entry.name == "CCTV-8" and bool(re.search(r"cctv[-_]?8k|cctv8k|8k", (entry.url + " " + tested_url).lower()))
        if suspected_identity:
            row["status"] = "suspected_identity"
        elif row["height"] <= 0:
            row["status"] = "unknown"
        else:
            row["status"] = "success"
        required = 2160 if entry.name == "CCTV-4K" else 1080 if row["is_core"] else 720
        if row["height"] < required:
            row["quality_gate"] = f"fail_height<{required}"
        elif row["is_core"] and row["codec"].lower() in {"h264", "avc", "avc1"} and row["stream_mbps"] and row["stream_mbps"] < 2.0:
            row["quality_gate"] = "fail_fake_1080_bitrate"
        elif row["headroom"] and row["headroom"] < (1.5 if row["is_core"] else 1.35):
            row["quality_gate"] = "fail_headroom"
        elif row["short_lived_token"]:
            row["quality_gate"] = "fail_short_token"
        elif row["is_master_playlist"] and row["has_480_540_576_720_variant"]:
            row["quality_gate"] = "fail_unlocked_adaptive_master"
        elif row["status"] == "suspected_identity":
            row["quality_gate"] = "fail_suspected_identity"
        else:
            row["quality_gate"] = "pass"
    except subprocess.TimeoutExpired:
        row["status"] = "failed"; row["quality_gate"] = "fail_unreachable"; row["error"] = f"ffprobe timeout after {timeout}s"
    except Exception as exc:
        row["status"] = "failed"; row["quality_gate"] = "fail_unreachable"; row["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"2160_plus": 0, "1080": 0, "720": 0, "below_720": 0, "unknown": 0, "unreachable": 0}
    for row in rows:
        if row["status"] == "failed": counts["unreachable"] += 1
        else: counts[resolution_bucket(int(row.get("height") or 0))] += 1
    core_bad = [row["name"] for row in rows if row["is_core"] and row["quality_gate"] != "pass"]
    low = [row["name"] for row in rows if 0 < int(row["height"] or 0) < (2160 if row["name"] == "CCTV-4K" else 1080 if row["is_core"] else 720)]
    unknown = [row["name"] for row in rows if row["status"] == "unknown"]
    dead = [row["name"] for row in rows if row["status"] == "failed"]
    suspected = [row["name"] for row in rows if row["status"] == "suspected_identity"]
    return {"channels": len(rows), "counts": counts, "core_not_acceptable": core_bad, "low_resolution": low,
            "unknown": unknown, "unreachable": dead, "suspected_identity": suspected,
            "final_version_accepted_count": 0, "promotion_ready": not core_bad and not suspected}


def write_outputs(out_dir: Path, rows: list[dict[str, Any]], playlist: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows)
    payload = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "playlist": str(playlist),
               "resolution_source": "ffprobe decoded video stream; HLS RESOLUTION labels are not accepted as proof",
               "summary": summary, "results": rows}
    (out_dir / "quality-report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["index", "name", "group", "url", "final_url", "is_core", "width", "height", "codec", "fps", "scan",
              "stream_mbps", "download_mbps_samples", "worst_download_mbps", "startup_seconds_samples", "worst_startup_seconds",
              "headroom", "is_master_playlist", "tested_variant_url", "variant_actual_heights", "has_480_540_576_720_variant",
              "short_lived_token", "status", "quality_gate", "final_version_accepted", "error"]
    with (out_dir / "quality-report.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
    lines = [f"playlist={playlist}", f"channels={summary['channels']}", *(f"{k}={v}" for k, v in summary["counts"].items()),
             "final_version_accepted_count=0", "core_not_acceptable=" + ", ".join(summary["core_not_acceptable"]),
             "low_resolution=" + ", ".join(summary["low_resolution"]), "unknown=" + ", ".join(summary["unknown"]),
             "unreachable=" + ", ".join(summary["unreachable"]), "suspected_identity=" + ", ".join(summary["suspected_identity"])]
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", type=Path, default=Path("tv-easy.m3u"))
    parser.add_argument("--out-dir", type=Path, default=Path("production-quality-audit"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=14.0)
    parser.add_argument("--speed-samples", type=int, default=3)
    args = parser.parse_args()
    entries = parse_playlist(args.playlist)
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(audit_entry, entry, args.timeout, max(2, args.speed_samples)): entry for entry in entries}
        for future in concurrent.futures.as_completed(futures):
            row = future.result(); rows.append(row)
            print(f"{row['index']:03d} {row['name']} {row['status']} {row['width']}x{row['height']} {row['codec']} gate={row['quality_gate']}", flush=True)
    rows.sort(key=lambda row: int(row["index"]))
    summary = write_outputs(args.out_dir, rows, args.playlist)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
