#!/usr/bin/env python3
"""Promote verified 1080p50 or native 1080i50 CCTV/satellite routes.

Chinese broadcast HD is commonly 1080i50. ffprobe often reports that as
r_frame_rate=50, avg_frame_rate=25 plus an interlaced field_order. That still
preserves 50 Hz motion sampling and is preferable to a platform-side 25p
conversion for sports, scrolling text and camera pans.

This script reuses the candidate mining/HLS speed code from upgrade_core_50fps
but applies the correct 50 Hz broadcast test. Channel names remain untouched;
only the winning URL is replaced.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import subprocess
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass

import upgrade_core_50fps as base


@dataclass
class HzResult:
    candidate: base.Candidate
    ok: bool = False
    width: int = 0
    height: int = 0
    avg_fps: float = 0.0
    r_fps: float = 0.0
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
        return self.height >= 1080 and field in {"", "unknown", "progressive"} and max(self.avg_fps, self.r_fps) >= 49.0

    @property
    def interlaced50(self) -> bool:
        field = self.field_order.lower()
        return (
            self.height >= 1080
            and field not in {"", "unknown", "progressive"}
            and self.r_fps >= 49.0
            and self.avg_fps >= 24.0
        )

    @property
    def temporal50(self) -> bool:
        return self.progressive50 or self.interlaced50

    @property
    def headroom_ok(self) -> bool:
        need = max(6.0, self.stream_mbps * 1.35 if self.stream_mbps else 6.0)
        return self.speed_mbps >= need and self.startup_s <= 4.0

    @property
    def promotable(self) -> bool:
        return self.ok and self.temporal50 and self.headroom_ok


def probe_meta(url: str) -> tuple[int, int, float, float, str, str, float]:
    cmd = [
        "ffprobe", "-v", "error", "-rw_timeout", "7000000",
        "-probesize", "2500000", "-analyzeduration", "3500000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,field_order,codec_name,bit_rate",
        "-show_entries", "format=bit_rate", "-of", "json", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=base.FFPROBE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe_failed")[-180:])
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
        base.rate(stream.get("avg_frame_rate")),
        base.rate(stream.get("r_frame_rate")),
        str(stream.get("field_order") or ""),
        str(stream.get("codec_name") or ""),
        bitrate,
    )


def evaluate(candidate: base.Candidate) -> HzResult:
    result = HzResult(candidate)
    try:
        width, height, avg_fps, r_fps, field, codec, bitrate = probe_meta(candidate.url)
        speed, stream, startup, manifest_fps = base.hls_speed(candidate.url)
        if not avg_fps and manifest_fps:
            avg_fps = manifest_fps
        if not r_fps and manifest_fps:
            r_fps = manifest_fps
        result.ok = True
        result.width = width
        result.height = height
        result.avg_fps = avg_fps
        result.r_fps = r_fps
        result.field_order = field
        result.codec = codec
        result.bitrate_mbps = bitrate
        result.speed_mbps = speed
        result.stream_mbps = stream or bitrate
        result.startup_s = startup
    except Exception as exc:
        result.error = f"{type(exc).__name__}:{str(exc)[:160]}"
    return result


def score(result: HzResult) -> float:
    if not result.ok:
        return -1e9
    value = 0.0
    if result.progressive50:
        value += 1550
    elif result.interlaced50:
        value += 1400
    if result.height >= 1080:
        value += 500
    elif result.height >= 720:
        value += 100
    value += min(result.speed_mbps, 40) * 6
    value += min(result.stream_mbps, 15) * 18
    value -= result.startup_s * 28
    if result.candidate.hint50:
        value += 20
    if result.candidate.current:
        value += 10
    return value


def main() -> int:
    candidates = base.current_candidates()
    source_status: list[str] = []
    for source, url in base.SOURCES:
        try:
            parsed = base.parse_source(base.fetch_text(url), source)
            candidates.extend(parsed)
            source_status.append(f"{source}:ok:{len(parsed)}")
        except Exception as exc:
            source_status.append(f"{source}:fail:{type(exc).__name__}")

    unique: dict[tuple[str, str], base.Candidate] = {}
    for candidate in candidates:
        marker = (candidate.key, candidate.url)
        old = unique.get(marker)
        if old is None or base.prelim_rank(candidate) > base.prelim_rank(old):
            unique[marker] = candidate

    by_key: dict[str, list[base.Candidate]] = defaultdict(list)
    for candidate in unique.values():
        by_key[candidate.key].append(candidate)

    probe_list: list[base.Candidate] = []
    for _, items in by_key.items():
        items.sort(key=base.prelim_rank, reverse=True)
        current = [item for item in items if item.current]
        labelled = [item for item in items if item.hint50 or re.search(r"50\s*fps|50p", item.name, re.I)]
        ordinary = [item for item in items if item not in current and item not in labelled]
        selected: list[base.Candidate] = []
        host_counts: Counter[str] = Counter()
        for item in current + labelled + ordinary:
            host = (urllib.parse.urlsplit(item.url).hostname or "").lower()
            if item not in current and host_counts[host] >= 2:
                continue
            selected.append(item)
            host_counts[host] += 1
            if len(selected) >= base.MAX_PER_KEY:
                break
        probe_list.extend(selected)

    results: list[HzResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=base.WORKERS) as executor:
        future_map = {executor.submit(evaluate, candidate): candidate for candidate in probe_list}
        for future in concurrent.futures.as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(HzResult(future_map[future], error=f"executor:{type(exc).__name__}"))

    by_result: dict[str, list[HzResult]] = defaultdict(list)
    for result in results:
        by_result[result.candidate.key].append(result)
    for items in by_result.values():
        items.sort(key=score, reverse=True)

    chosen: dict[str, HzResult] = {}
    promotion_hosts: Counter[str] = Counter()
    for key in sorted(by_result):
        current_ok = [r for r in by_result[key] if r.candidate.current and r.ok]
        current_best = max(current_ok, key=score, default=None)
        for result in by_result[key]:
            if not result.promotable:
                continue
            if result.candidate.current:
                break
            if promotion_hosts[result.host] >= base.HOST_PROMOTION_CAP:
                continue
            try:
                speed2, stream2, startup2, _ = base.hls_speed(result.candidate.url)
            except Exception:
                continue
            stream_ref = stream2 or result.stream_mbps
            if speed2 < max(5.0, stream_ref * 1.25 if stream_ref else 5.0) or startup2 > 4.5:
                continue
            result.speed_mbps = min(result.speed_mbps, speed2)
            if stream2:
                result.stream_mbps = min(result.stream_mbps, stream2) if result.stream_mbps else stream2
            result.startup_s = max(result.startup_s, startup2)
            if not result.headroom_ok:
                continue
            # Keep an already-good current 50 Hz source unless the newcomer is
            # clearly better. Avoid churn from tiny benchmark differences.
            if current_best and current_best.temporal50 and score(result) < score(current_best) + 80:
                break
            chosen[key] = result
            promotion_hosts[result.host] += 1
            break

    replacements = {name: base.replace_urls(base.Path(name), chosen) for name in base.PLAYLISTS}
    verified = [r for r in results if r.promotable]
    report_lines = [
        f"generated_utc={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        "selection_mode=1080p50_then_native_1080i50",
        "source_status=" + json.dumps(source_status, ensure_ascii=False),
        f"candidate_routes={len(unique)}",
        f"probed_routes={len(results)}",
        f"probe_ok={sum(r.ok for r in results)}",
        f"verified_1080_50hz={len(verified)}",
        f"verified_1080p50={sum(r.progressive50 and r.headroom_ok for r in results)}",
        f"verified_1080i50={sum(r.interlaced50 and r.headroom_ok for r in results)}",
        f"promoted_stations={len(chosen)}",
        "promotion_hosts=" + json.dumps(dict(promotion_hosts), ensure_ascii=False, sort_keys=True),
        "playlist_replacements=" + json.dumps(replacements, ensure_ascii=False, sort_keys=True),
        "promotions=" + json.dumps([
            {
                "key": key,
                "url": result.candidate.url,
                "source": result.candidate.source,
                "host": result.host,
                "width": result.width,
                "height": result.height,
                "avg_fps": round(result.avg_fps, 3),
                "r_fps": round(result.r_fps, 3),
                "field_order": result.field_order,
                "mode": "1080p50" if result.progressive50 else "1080i50",
                "codec": result.codec,
                "download_mbps": round(result.speed_mbps, 2),
                "stream_mbps": round(result.stream_mbps, 2),
                "startup_s": round(result.startup_s, 3),
            }
            for key, result in sorted(chosen.items())
        ], ensure_ascii=False),
        "top_verified_50hz=" + json.dumps([
            {
                "key": r.candidate.key,
                "source": r.candidate.source,
                "host": r.host,
                "mode": "1080p50" if r.progressive50 else "1080i50",
                "avg_fps": round(r.avg_fps, 3),
                "r_fps": round(r.r_fps, 3),
                "field_order": r.field_order,
                "height": r.height,
                "download_mbps": round(r.speed_mbps, 2),
                "stream_mbps": round(r.stream_mbps, 2),
                "startup_s": round(r.startup_s, 3),
                "url": r.candidate.url,
            }
            for r in sorted(verified, key=score, reverse=True)[:100]
        ], ensure_ascii=False),
    ]
    base.REPORT.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n".join(report_lines[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
