#!/opt/bin/python3
"""Single-threaded home IPTV probe designed for an Asus RT-AC86U.

The router runs one primary pass at 02:00 Beijing time and one formal-route
recheck at 13:00.  There is no six-hour cadence and no three-day rotation.
Both passes measure the formal CCTV/satellite routes from the living-room
network path; only the 02:00 pass consumes newly discovered GitHub candidates.
The process never edits a production playlist.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .home_contract import (
        ContractError,
        canonical_name,
        station_key,
        validate_candidate_manifest,
    )
except ImportError:  # Installed beside this file by the Entware installer.
    from home_contract import (  # type: ignore
        ContractError,
        canonical_name,
        station_key,
        validate_candidate_manifest,
    )


USER_AGENT = "Mozilla/5.0 (AC86U-IPTV-Home-Probe/1.0)"
DEFAULT_PLAYLIST = "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u"
DEFAULT_CANDIDATE_MANIFEST = "https://raw.githubusercontent.com/pppaaasss/-/master/harvest/home-candidates.json"
LIGHT_SAMPLE_BYTES = 2 * 1024 * 1024
CANDIDATE_SAMPLE_BYTES = 6 * 1024 * 1024
SAMPLES_PER_ROUTE = 2
MIN_SAMPLE_BYTES = 64 * 1024
PLAYLIST_LIMIT = 1024 * 1024
CANDIDATE_MANIFEST_LIMIT = 4 * 1024 * 1024
HTTP_TIMEOUT = 10.0
FFPROBE_TIMEOUT = 18
STATUSES = {"GOOD", "DEGRADED", "UNKNOWN", "DEAD"}
RUN_KINDS = {"primary-0200", "recheck-1300"}


def utc_text(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else float(epoch)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def parse_utc(raw: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default or {})
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def system_resources() -> dict:
    load1 = 0.0
    mem_available = 0
    try:
        load1 = float(Path("/proc/loadavg").read_text(encoding="ascii").split()[0])
    except Exception:
        try:
            load1 = float(os.getloadavg()[0])
        except Exception:
            pass
    try:
        for raw in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if raw.startswith("MemAvailable:"):
                mem_available = int(raw.split()[1])
                break
    except Exception:
        pass
    return {"load1": round(load1, 3), "mem_available_kib": mem_available}


def resource_guard(resources: dict, config: dict) -> str:
    max_load = float(config.get("maximum_load1") or 1.5)
    min_memory = int(config.get("minimum_mem_available_kib") or 64 * 1024)
    if float(resources.get("load1") or 0) > max_load:
        return f"load1_above_{max_load:g}"
    available = int(resources.get("mem_available_kib") or 0)
    if available and available < min_memory:
        return f"memory_below_{min_memory}_kib"
    return ""


def timezone_guard(config: dict, now_epoch: float) -> None:
    expected = str(config.get("expected_utc_offset") or "").strip()
    if not expected:
        return
    actual = time.strftime("%z", time.localtime(now_epoch))
    if actual != expected:
        raise RuntimeError(f"TIMEZONE_MISMATCH:expected_{expected}_got_{actual or 'unknown'}")


def request_bytes(url: str, limit: int, *, ranged: bool = False) -> tuple[bytes, str, float, int, bool]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"}
    if ranged:
        headers["Range"] = f"bytes=0-{max(0, limit - 1)}"
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        data = response.read(limit)
        elapsed = max(time.monotonic() - started, 0.001)
        status = int(getattr(response, "status", 0) or response.getcode() or 0)
        total = 0
        content_range = str(response.headers.get("Content-Range") or "")
        match = re.search(r"/(\d+)$", content_range)
        if match:
            total = int(match.group(1))
        if not total:
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
        complete = bool(total and total <= limit and len(data) >= total)
        if not total and len(data) < limit:
            total = len(data)
            complete = True
        if status == 206 and total == len(data):
            complete = True
        return data, response.geturl(), elapsed, total, complete


def fetch_playlist(url: str) -> tuple[bytes, str, float]:
    data, final, elapsed, _, _ = request_bytes(url, PLAYLIST_LIMIT)
    if not data:
        raise RuntimeError("empty_playlist")
    return data, final, elapsed


def fetch_candidate_manifest(url: str, *, now_epoch: float, max_age_hours: float) -> tuple[dict, bytes, str]:
    data, final, _, total, complete = request_bytes(url, CANDIDATE_MANIFEST_LIMIT)
    if not data:
        raise RuntimeError("empty_candidate_manifest")
    if total > CANDIDATE_MANIFEST_LIMIT or (len(data) >= CANDIDATE_MANIFEST_LIMIT and not complete):
        raise RuntimeError("candidate_manifest_too_large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("invalid_candidate_manifest_json") from exc
    try:
        validated = validate_candidate_manifest(
            payload,
            now_epoch=now_epoch,
            max_age_hours=max_age_hours,
        )
    except ContractError as exc:
        raise RuntimeError(f"unsafe_candidate_manifest:{exc}") from exc
    return validated, data, final


def run_profile(run_kind: str, config: dict) -> dict:
    if run_kind not in RUN_KINDS:
        raise RuntimeError(f"unsupported_run_kind:{run_kind}")
    primary = run_kind == "primary-0200"
    key = "primary_sample_bytes" if primary else "recheck_sample_bytes"
    return {
        "run_kind": run_kind,
        "scan_candidates": primary,
        "current_sample_bytes": int(config.get(key) or LIGHT_SAMPLE_BYTES),
        "candidate_sample_bytes": int(config.get("candidate_sample_bytes") or CANDIDATE_SAMPLE_BYTES),
        "current_metadata": True,
        "candidate_metadata": True,
    }


def parse_playlist(data: bytes) -> list[tuple[str, str]]:
    lines = data.decode("utf-8", "ignore").splitlines()
    rows: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line.startswith("#EXTINF") or "," not in line:
            continue
        name = line.rsplit(",", 1)[-1].strip()
        cursor = index + 1
        while cursor < len(lines) and (not lines[cursor].strip() or lines[cursor].lstrip().startswith("#")):
            cursor += 1
        if cursor >= len(lines):
            continue
        url = lines[cursor].strip()
        if not url.startswith(("http://", "https://")):
            continue
        if name in seen and seen[name] != url:
            raise RuntimeError(f"conflicting_duplicate_channel:{name}")
        if name not in seen:
            rows.append((name, url))
            seen[name] = url
    if not rows:
        raise RuntimeError("playlist_has_no_http_channels")
    return rows


def merge_candidate_queue(
    previous: object,
    incoming: list[dict],
    current_urls: dict[str, str],
) -> list[dict]:
    """Merge daily candidates into a persistent, deterministic router queue."""
    merged: dict[str, dict] = {}
    if isinstance(previous, list):
        for row in previous:
            if isinstance(row, dict) and str(row.get("candidate_id") or ""):
                merged[str(row["candidate_id"])] = dict(row)
    for row in incoming:
        if isinstance(row, dict) and str(row.get("candidate_id") or ""):
            merged[str(row["candidate_id"])] = dict(row)
    output = []
    for identity, row in merged.items():
        key = str(row.get("channel_key") or "")
        url = str(row.get("url") or "")
        if key not in current_urls or not url or url == current_urls[key]:
            continue
        row["candidate_id"] = identity
        output.append(row)
    return sorted(output, key=lambda row: (str(row.get("channel_key")), str(row.get("candidate_id"))))


def parse_rate(raw: str | None) -> float:
    if not raw or raw in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            return float(numerator) / float(denominator) if float(denominator) else 0.0
        return float(raw)
    except Exception:
        return 0.0


def choose_variant(text: str, base: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
        height = int(resolution.group(2)) if resolution else 0
        bitrate = int(bandwidth.group(1)) if bandwidth else 0
        for following in lines[index + 1 :]:
            if not following.startswith("#"):
                variants.append((height, bitrate, urllib.parse.urljoin(base, following)))
                break
    return max(variants, key=lambda item: (item[0], item[1]))[2] if variants else None


def recent_segments(text: str, base: str, count: int = SAMPLES_PER_ROUTE) -> list[tuple[str, float]]:
    segments: list[tuple[str, float]] = []
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF:"):
            match = re.match(r"#EXTINF:([0-9.]+)", line, re.I)
            duration = float(match.group(1)) if match else 0.0
        elif line and not line.startswith("#"):
            segments.append((urllib.parse.urljoin(base, line), duration))
            duration = 0.0
    selected: list[tuple[str, float]] = []
    seen: set[str] = set()
    for segment in reversed(segments):
        if segment[0] in seen:
            continue
        selected.append(segment)
        seen.add(segment[0])
        if len(selected) >= count:
            break
    selected.reverse()
    return selected


def segment_sample(url: str, duration: float, limit: int) -> dict:
    data, final, elapsed, total, complete = request_bytes(url, limit, ranged=True)
    download = len(data) * 8 / elapsed / 1_000_000 if data else 0.0
    stream = total * 8 / duration / 1_000_000 if total > 0 and duration > 0 else 0.0
    return {
        "url": final,
        "downloaded_bytes": len(data),
        "total_bytes": total,
        "duration_s": round(max(0.0, duration), 3),
        "elapsed_s": round(elapsed, 3),
        "download_mbps": round(download, 3),
        "stream_mbps": round(stream, 3),
        "complete": bool(complete),
    }


def ffprobe_meta(url: str, ffprobe: str) -> dict:
    command = [
        ffprobe,
        "-v", "error",
        "-rw_timeout", "8000000",
        "-probesize", "2000000",
        "-analyzeduration", "4000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,avg_frame_rate,r_frame_rate,bit_rate",
        "-show_entries", "format=bit_rate",
        "-of", "json",
        url,
    ]
    process = subprocess.run(command, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    if process.returncode != 0:
        raise RuntimeError((process.stderr or "ffprobe_failed").strip()[-240:])
    payload = json.loads(process.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("no_video_stream")
    stream = streams[0]
    bitrate = 0.0
    for raw in (stream.get("bit_rate"), (payload.get("format") or {}).get("bit_rate")):
        try:
            bitrate = max(bitrate, float(raw or 0) / 1_000_000)
        except Exception:
            pass
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": str(stream.get("codec_name") or ""),
        "fps": round(max(parse_rate(stream.get("avg_frame_rate")), parse_rate(stream.get("r_frame_rate"))), 3),
        "bitrate_mbps": round(bitrate, 3),
    }


def empty_result(name: str, url: str, floor: int) -> dict:
    return {
        "name": name,
        "url": url,
        "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
        "status": "UNKNOWN",
        "observed_status": "UNKNOWN",
        "sample_count": 0,
        "segment_samples": [],
        "startup_s": 0.0,
        "min_download_mbps": 0.0,
        "avg_download_mbps": 0.0,
        "stream_mbps": 0.0,
        "headroom_ratio": 0.0,
        "width": 0,
        "height": 0,
        "codec": "",
        "fps": 0.0,
        "bitrate_mbps": 0.0,
        "min_height": int(floor),
        "deep_checked": False,
        "error": "",
        "consecutive_failures": 0,
        "failure_age_hours": 0.0,
        "home_dead_confirmed": False,
        "consecutive_degraded": 0,
        "degraded_age_hours": 0.0,
        "home_degraded_confirmed": False,
    }


def probe_route(
    name: str,
    url: str,
    *,
    floor: int,
    config: dict,
    sample_limit: int | None = None,
    include_metadata: bool | None = None,
    mode: str | None = None,
) -> dict:
    result = empty_result(name, url, floor)
    # ``mode`` remains a compatibility shim for local tests and manual calls;
    # scheduled work is driven only by the explicit 02:00/13:00 run profile.
    if sample_limit is None:
        sample_limit = int(config.get("candidate_sample_bytes") or CANDIDATE_SAMPLE_BYTES) if mode == "deep" else int(
            config.get("primary_sample_bytes") or config.get("light_sample_bytes") or LIGHT_SAMPLE_BYTES
        )
    if include_metadata is None:
        include_metadata = mode == "deep"
    limit = int(sample_limit)
    started = time.monotonic()
    try:
        document, final, manifest_s = fetch_playlist(url)
        text = document.decode("utf-8", "ignore")
        samples: list[dict] = []
        if "#EXTM3U" in text:
            child = choose_variant(text, final)
            if child:
                child_data, final, child_s = fetch_playlist(child)
                manifest_s += child_s
                text = child_data.decode("utf-8", "ignore")
                if "#EXTM3U" not in text:
                    raise RuntimeError("bad_child_playlist")
            segments = recent_segments(text, final, SAMPLES_PER_ROUTE)
            if len(segments) < SAMPLES_PER_ROUTE:
                raise RuntimeError("fewer_than_two_distinct_segments")
            for segment_url, duration in segments:
                samples.append(segment_sample(segment_url, duration, limit))
        else:
            # Some formal routes are long-lived MPEG-TS/FLV HTTP streams.  Two
            # independent bounded reads still prove repeatable home reachability.
            for _ in range(SAMPLES_PER_ROUTE):
                samples.append(segment_sample(url, 0.0, limit))

        result["segment_samples"] = samples
        result["sample_count"] = len(samples)
        result["startup_s"] = round(manifest_s + (samples[0]["elapsed_s"] if samples else 0), 3)
        speeds = [float(sample["download_mbps"]) for sample in samples]
        streams = [float(sample["stream_mbps"]) for sample in samples if float(sample["stream_mbps"]) > 0]
        result["min_download_mbps"] = round(min(speeds), 3) if speeds else 0.0
        result["avg_download_mbps"] = round(sum(speeds) / len(speeds), 3) if speeds else 0.0
        result["stream_mbps"] = round(sum(streams) / len(streams), 3) if streams else 0.0
        if result["stream_mbps"] > 0:
            result["headroom_ratio"] = round(result["min_download_mbps"] / result["stream_mbps"], 3)
        if len(samples) != SAMPLES_PER_ROUTE or any(
            int(sample["downloaded_bytes"]) < int(config.get("minimum_sample_bytes") or MIN_SAMPLE_BYTES)
            for sample in samples
        ):
            raise RuntimeError("short_segment_sample")

        result["observed_status"] = "GOOD"
        minimum_headroom = float(config.get("minimum_headroom_ratio") or 1.35)
        if result["headroom_ratio"] and result["headroom_ratio"] < minimum_headroom:
            result["observed_status"] = "DEGRADED"
            result["error"] = f"headroom_{result['headroom_ratio']:.3f}_below_{minimum_headroom:.3f}"

        if include_metadata:
            try:
                meta = ffprobe_meta(url, str(config.get("ffprobe") or "/opt/bin/ffprobe"))
                result.update(meta)
                result["deep_checked"] = True
                if result["height"] < floor:
                    result["observed_status"] = "DEGRADED"
                    result["error"] = f"decoded_height_{result['height']}_below_{floor}"
                intrinsic = result["stream_mbps"] or result["bitrate_mbps"]
                minimum_h264 = float(config.get("minimum_h264_stream_mbps") or 5.0)
                if result["codec"].casefold() == "h264" and 0 < intrinsic < minimum_h264:
                    result["observed_status"] = "DEGRADED"
                    result["error"] = f"h264_stream_{intrinsic:.3f}_below_{minimum_h264:.3f}_mbps"
            except Exception as exc:
                # Segment evidence remains useful.  Missing ffprobe metadata is
                # quality-unknown, not proof that a playing route is dead.
                result["error"] = (result["error"] + ";" if result["error"] else "") + (
                    f"quality_unknown:{type(exc).__name__}:{str(exc)[:180]}"
                )
        result["status"] = result["observed_status"]
    except Exception as exc:
        result["observed_status"] = "UNKNOWN"
        result["status"] = "UNKNOWN"
        result["error"] = f"{type(exc).__name__}:{str(exc)[:240]}"
    result["probe_runtime_s"] = round(time.monotonic() - started, 3)
    return result


def quality_cache(result: dict) -> dict:
    return {
        "checked_utc": result.get("checked_utc", ""),
        "status": result.get("observed_status"),
        "height": int(result.get("height") or 0),
        "width": int(result.get("width") or 0),
        "codec": str(result.get("codec") or ""),
        "fps": float(result.get("fps") or 0),
        "bitrate_mbps": float(result.get("bitrate_mbps") or 0),
        "stream_mbps": float(result.get("stream_mbps") or 0),
        "error": str(result.get("error") or ""),
    }


def apply_cached_quality(result: dict, previous: dict, now_epoch: float, config: dict) -> None:
    if result.get("observed_status") != "GOOD" or str(previous.get("last_url") or "") != result["url"]:
        return
    cached = previous.get("last_deep") if isinstance(previous.get("last_deep"), dict) else {}
    checked = parse_utc(str(cached.get("checked_utc") or ""))
    maximum_age = float(config.get("quality_cache_hours") or 96) * 3600
    if checked is None or now_epoch - checked < 0 or now_epoch - checked > maximum_age:
        return
    for field in ("height", "width", "codec", "fps", "bitrate_mbps"):
        if not result.get(field) and cached.get(field):
            result[field] = cached[field]
    if cached.get("status") == "DEGRADED":
        result["observed_status"] = "DEGRADED"
        result["status"] = "DEGRADED"
        result["error"] = f"cached_deep:{str(cached.get('error') or 'quality_degraded')[:220]}"


def _age_hours(first: str, now_epoch: float) -> float:
    epoch = parse_utc(first)
    return round(max(0.0, now_epoch - epoch) / 3600, 3) if epoch is not None else 0.0


def update_current_state(
    results: list[dict],
    previous_state: dict,
    *,
    now_epoch: float,
    mode: str,
    config: dict,
) -> tuple[dict, bool]:
    now = utc_text(now_epoch)
    old_channels = previous_state.get("channels") if isinstance(previous_state.get("channels"), dict) else {}
    unknown = sum(row.get("observed_status") == "UNKNOWN" for row in results)
    ratio = unknown / len(results) if results else 1.0
    circuit = unknown >= int(config.get("circuit_breaker_min_unknown") or 12) and ratio >= float(
        config.get("circuit_breaker_unknown_ratio") or 0.35
    )
    channels: dict[str, dict] = {}
    dead_runs = int(config.get("dead_after_runs") or 3)
    dead_hours = float(config.get("dead_min_age_hours") or 6)
    degraded_runs = int(config.get("degraded_after_runs") or 3)
    degraded_hours = float(config.get("degraded_min_age_hours") or 6)

    for row in results:
        previous = old_channels.get(row["name"]) if isinstance(old_channels.get(row["name"]), dict) else {}
        same_url = str(previous.get("last_url") or "") == row["url"]
        if mode != "deep":
            apply_cached_quality(row, previous if same_url else {}, now_epoch, config)
        observed = str(row["observed_status"])
        failures = int(previous.get("consecutive_failures") or 0) if same_url else 0
        degraded = int(previous.get("consecutive_degraded") or 0) if same_url else 0
        first_failure = str(previous.get("first_failure_utc") or "") if same_url else ""
        first_degraded = str(previous.get("first_degraded_utc") or "") if same_url else ""

        if not circuit:
            if observed == "UNKNOWN":
                failures += 1
                first_failure = first_failure or now
            else:
                failures = 0
                first_failure = ""
            if observed == "DEGRADED":
                degraded += 1
                first_degraded = first_degraded or now
            elif observed == "GOOD":
                degraded = 0
                first_degraded = ""

        failure_age = _age_hours(first_failure, now_epoch)
        degraded_age = _age_hours(first_degraded, now_epoch)
        was_dead = same_url and bool(previous.get("home_dead_confirmed"))
        was_degraded = same_url and bool(previous.get("home_degraded_confirmed"))
        dead_confirmed = was_dead or (
            not circuit and observed == "UNKNOWN" and failures >= dead_runs and failure_age >= dead_hours
        )
        degraded_confirmed = was_degraded or (
            not circuit and observed == "DEGRADED" and degraded >= degraded_runs and degraded_age >= degraded_hours
        )
        if observed == "GOOD":
            dead_confirmed = False
            degraded_confirmed = False
        elif observed == "DEGRADED":
            dead_confirmed = False

        row["consecutive_failures"] = failures
        row["failure_age_hours"] = failure_age
        row["home_dead_confirmed"] = bool(dead_confirmed)
        row["consecutive_degraded"] = degraded
        row["degraded_age_hours"] = degraded_age
        row["home_degraded_confirmed"] = bool(degraded_confirmed)
        row["status"] = "DEAD" if dead_confirmed else observed
        last_deep = previous.get("last_deep") if same_url and isinstance(previous.get("last_deep"), dict) else {}
        if mode == "deep" and observed in {"GOOD", "DEGRADED"} and row.get("deep_checked"):
            row["checked_utc"] = now
            last_deep = quality_cache(row)
        channels[row["name"]] = {
            "last_url": row["url"],
            "status": row["status"],
            "observed_status": observed,
            "consecutive_failures": failures,
            "first_failure_utc": first_failure,
            "consecutive_degraded": degraded,
            "first_degraded_utc": first_degraded,
            "home_dead_confirmed": bool(dead_confirmed),
            "home_degraded_confirmed": bool(degraded_confirmed),
            "last_deep": last_deep,
            "updated_utc": now,
        }
    state = dict(previous_state)
    state.update({
        "version": 1,
        "last_run_utc": now,
        "last_mode": mode,
        "last_deep_utc": now if mode == "deep" else str(previous_state.get("last_deep_utc") or ""),
        "channels": channels,
        "circuit_breaker_open": circuit,
    })
    return state, circuit


def update_candidate_state(
    results: list[dict],
    state: dict,
    *,
    now_epoch: float,
    config: dict,
) -> dict:
    now = utc_text(now_epoch)
    old = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
    updated = dict(old)
    required_runs = int(config.get("candidate_good_after_runs") or 2)
    required_hours = float(config.get("candidate_good_min_age_hours") or 6)
    for row in results:
        previous = old.get(row["name"]) if isinstance(old.get(row["name"]), dict) else {}
        same_url = str(previous.get("last_url") or "") == row["url"]
        runs = int(previous.get("consecutive_good") or 0) if same_url else 0
        first = str(previous.get("first_good_utc") or "") if same_url else ""
        fully_qualified = (
            row.get("observed_status") == "GOOD"
            and row.get("deep_checked") is True
            and int(row.get("height") or 0) >= int(row.get("min_height") or 0)
            and int(row.get("sample_count") or 0) == SAMPLES_PER_ROUTE
        )
        if fully_qualified:
            runs += 1
            first = first or now
        else:
            runs = 0
            first = ""
        age = _age_hours(first, now_epoch)
        confirmed = bool(fully_qualified and runs >= required_runs and age >= required_hours)
        row["candidate_confirmed"] = confirmed
        updated[row["name"]] = {
            "last_url": row["url"],
            "consecutive_good": runs,
            "first_good_utc": first,
            "candidate_confirmed": confirmed,
            "updated_utc": now,
        }
    state["candidates"] = updated
    return state


def minimum_height(name: str, config: dict) -> int:
    overrides = config.get("minimum_height_overrides") if isinstance(config.get("minimum_height_overrides"), dict) else {}
    return int(overrides.get(name) or (2160 if name == "CCTV-4K" else config.get("minimum_height_default") or 1080))


def candidate_qualification(result: dict) -> str:
    if result.get("observed_status") == "UNKNOWN":
        return "UNKNOWN"
    if (
        result.get("observed_status") == "GOOD"
        and result.get("deep_checked") is True
        and int(result.get("height") or 0) >= int(result.get("min_height") or 0)
        and int(result.get("sample_count") or 0) == SAMPLES_PER_ROUTE
    ):
        return "QUALIFIED"
    return "REJECTED"


def run(
    config: dict,
    *,
    run_kind: str = "primary-0200",
    now_epoch: float | None = None,
    requested_mode: str | None = None,
) -> tuple[dict, dict]:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    started = time.monotonic()
    output_dir = Path(str(config.get("output_dir") or "/opt/var/lib/iptv-home-probe"))
    state_path = output_dir / "state.json"
    previous_state = load_json(state_path)
    profile = run_profile(run_kind, config)
    # Compatibility for pre-migration unit tests and explicit manual shadows.
    # Scheduled execution never passes requested_mode and has no 72-hour state.
    if requested_mode in {"light", "deep"}:
        profile["current_metadata"] = requested_mode == "deep"
        profile["current_sample_bytes"] = (
            int(config.get("candidate_sample_bytes") or CANDIDATE_SAMPLE_BYTES)
            if requested_mode == "deep"
            else int(config.get("primary_sample_bytes") or config.get("light_sample_bytes") or LIGHT_SAMPLE_BYTES)
        )
        profile["scan_candidates"] = False
    mode = "deep" if profile["current_metadata"] else "light"
    resources = system_resources()
    blocked = resource_guard(resources, config)
    if blocked:
        raise RuntimeError(f"RESOURCE_GUARD:{blocked}")
    timezone_guard(config, now_epoch)

    playlist_url = str(config.get("playlist_url") or DEFAULT_PLAYLIST)
    playlist_bytes, playlist_final, _ = fetch_playlist(playlist_url)
    entries = parse_playlist(playlist_bytes)
    maximum_runtime = float(config.get("maximum_runtime_s") or 3300)
    results: list[dict] = []
    for name, url in entries:
        if time.monotonic() - started >= maximum_runtime:
            raise RuntimeError("RUNTIME_BUDGET_EXHAUSTED")
        results.append(probe_route(
            name,
            url,
            floor=minimum_height(name, config),
            config=config,
            sample_limit=int(profile["current_sample_bytes"]),
            include_metadata=bool(profile["current_metadata"]),
        ))

    state, circuit = update_current_state(
        results,
        previous_state,
        now_epoch=now_epoch,
        mode=mode,
        config=config,
    )
    candidate_results: list[dict] = []
    candidate_playlist = None
    candidate_manifest_state = "not_requested"
    if profile["scan_candidates"]:
        candidate_url = str(config.get("candidate_manifest_url") or DEFAULT_CANDIDATE_MANIFEST).strip()
        if not candidate_url:
            candidate_manifest_state = "disabled"
        else:
            try:
                manifest, candidate_bytes, candidate_final = fetch_candidate_manifest(
                    candidate_url,
                    now_epoch=now_epoch,
                    max_age_hours=float(config.get("candidate_manifest_max_age_hours") or 48),
                )
                formal = manifest["formal_playlist"]
                formal_sha = hashlib.sha256(playlist_bytes).hexdigest()
                if formal["sha256"] != formal_sha or int(formal["channel_count"]) != len(entries):
                    raise RuntimeError("candidate_manifest_formal_playlist_changed")
                candidate_playlist = {
                    "url": candidate_final,
                    "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                    "channel_count": int(manifest["candidate_count"]),
                }
                current_urls = {
                    key: url
                    for name, url in entries
                    if (key := station_key(name)) is not None
                }
                queue = merge_candidate_queue(
                    previous_state.get("candidate_queue"),
                    list(manifest["candidates"]),
                    current_urls,
                )
                remaining: list[dict] = []
                observations = dict(previous_state.get("candidate_observations") or {}) if isinstance(
                    previous_state.get("candidate_observations"), dict
                ) else {}
                max_unknown_retries = max(1, int(config.get("candidate_unknown_retry_runs") or 2))
                for candidate in queue:
                    if time.monotonic() - started >= maximum_runtime:
                        remaining.append(candidate)
                        continue
                    key = str(candidate["channel_key"])
                    name = canonical_name(key)
                    row = probe_route(
                        name,
                        str(candidate["url"]),
                        floor=minimum_height(name, config),
                        config=config,
                        sample_limit=int(profile["candidate_sample_bytes"]),
                        include_metadata=bool(profile["candidate_metadata"]),
                    )
                    qualification = candidate_qualification(row)
                    row.update({
                        "candidate_id": candidate["candidate_id"],
                        "channel_key": key,
                        "request_options": str(candidate.get("request_options") or ""),
                        "qualification": qualification,
                        "purpose": "daily-qualification",
                        "switch_reverified": False,
                        "candidate_confirmed": qualification == "QUALIFIED",
                    })
                    candidate_results.append(row)
                    identity = str(candidate["candidate_id"])
                    old_observation = observations.get(identity) if isinstance(observations.get(identity), dict) else {}
                    unknown_attempts = int(old_observation.get("unknown_attempts") or 0)
                    unknown_attempts = unknown_attempts + 1 if qualification == "UNKNOWN" else 0
                    observations[identity] = {
                        "candidate": candidate,
                        "qualification": qualification,
                        "unknown_attempts": unknown_attempts,
                        "last_checked_utc": utc_text(now_epoch),
                        "result": row,
                    }
                    if qualification == "UNKNOWN" and unknown_attempts < max_unknown_retries:
                        remaining.append(candidate)
                state["candidate_queue"] = remaining
                state["candidate_observations"] = observations
                state["last_candidate_manifest_sha256"] = candidate_playlist["sha256"]
                state["last_candidate_manifest_utc"] = str(manifest["generated_utc"])
                candidate_manifest_state = "accepted"
            except Exception as exc:
                # A stale, racing, or malformed cloud manifest must never make
                # the formal home-health pass fail or mutate its existing queue.
                candidate_manifest_state = f"rejected:{type(exc).__name__}:{str(exc)[:220]}"
                state["candidate_queue"] = list(previous_state.get("candidate_queue") or [])
                state["candidate_observations"] = dict(previous_state.get("candidate_observations") or {})

    successful_runs = int(previous_state.get("successful_runs") or 0) + 1
    first_success = str(previous_state.get("first_success_utc") or "") or utc_text(now_epoch)
    state["successful_runs"] = successful_runs
    state["first_success_utc"] = first_success
    state["last_run_kind"] = run_kind
    state["candidate_manifest_state"] = candidate_manifest_state
    runtime = round(time.monotonic() - started, 3)
    resources["runtime_s"] = runtime
    summary = {
        "channels": len(results),
        "good": sum(row["status"] == "GOOD" for row in results),
        "degraded": sum(row["status"] == "DEGRADED" for row in results),
        "unknown": sum(row["status"] == "UNKNOWN" for row in results),
        "dead": sum(row["status"] == "DEAD" for row in results),
        "candidate_channels": len(candidate_results),
        "candidate_confirmed": sum(bool(row.get("candidate_confirmed")) for row in candidate_results),
        "candidate_queue_remaining": len(state.get("candidate_queue") or []),
        "circuit_breaker_open": bool(circuit),
    }
    report = {
        "schema": "iptv-home-probe/v1",
        "probe_id": str(config.get("probe_id") or "home-ac86u"),
        "generated_utc": utc_text(now_epoch),
        "run_status": "COMPLETED",
        "run_kind": run_kind,
        "mode": mode,
        "actionable": bool(config.get("actionable", False)),
        "production_modified": False,
        "probe_region": "home",
        "route_context": str(config.get("route_context") or "router-origin-direct-wan"),
        "playlist": {
            "url": playlist_final,
            "sha256": hashlib.sha256(playlist_bytes).hexdigest(),
            "channel_count": len(entries),
        },
        "candidate_playlist": candidate_playlist,
        "policy": {
            "auto_replace_formal_routes": False,
            "mass_failure_circuit_breaker": True,
            "single_threaded": True,
            "samples_per_route": SAMPLES_PER_ROUTE,
            "sample_bytes": int(profile["current_sample_bytes"]),
            "candidate_sample_bytes": int(profile["candidate_sample_bytes"]),
            "candidate_manifest_state": candidate_manifest_state,
            "dead_after_runs": int(config.get("dead_after_runs") or 3),
            "dead_min_age_hours": float(config.get("dead_min_age_hours") or 6),
            "degraded_after_runs": int(config.get("degraded_after_runs") or 3),
            "degraded_min_age_hours": float(config.get("degraded_min_age_hours") or 6),
            "candidate_requires_two_runs": False,
            "candidate_requires_two_samples_and_deep_metadata": True,
        },
        "resources": resources,
        "summary": summary,
        "results": results,
        "candidate_results": candidate_results,
    }
    atomic_json(output_dir / "latest.json", report)
    atomic_json(state_path, state)
    return report, state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    parser.add_argument("--run-kind", choices=tuple(sorted(RUN_KINDS)), default="primary-0200")
    parser.add_argument("--mode", choices=("light", "deep"), default=None, help=argparse.SUPPRESS)
    parser.add_argument("--now-epoch", type=float, default=None)
    args = parser.parse_args()
    try:
        config = load_json(Path(args.config))
        report, _ = run(
            config,
            run_kind=args.run_kind,
            requested_mode=args.mode,
            now_epoch=args.now_epoch,
        )
        summary = report["summary"]
        print(
            "HOME_PROBE completed "
            f"run_kind={report['run_kind']} mode={report['mode']} channels={summary['channels']} good={summary['good']} "
            f"degraded={summary['degraded']} unknown={summary['unknown']} dead={summary['dead']} "
            f"candidates={summary['candidate_confirmed']} circuit={int(summary['circuit_breaker_open'])}"
        )
        return 0
    except Exception as exc:
        message = str(exc)
        print(f"HOME_PROBE skipped_or_failed: {message}", file=sys.stderr)
        return 75 if message.startswith("RESOURCE_GUARD:") else 2


if __name__ == "__main__":
    raise SystemExit(main())
