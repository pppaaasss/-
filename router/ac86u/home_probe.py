#!/opt/bin/python3
"""Single-threaded home IPTV probe designed for an Asus RT-AC86U.

Light runs fetch two bounded 2 MiB samples per route.  Deep runs happen at
most once every three days, allow two complete segments up to 12 MiB each,
and ask ffprobe for container metadata.  The process never edits a playlist.
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


USER_AGENT = "Mozilla/5.0 (AC86U-IPTV-Home-Probe/1.0)"
DEFAULT_PLAYLIST = "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u"
DEFAULT_CANDIDATE_PLAYLIST = "https://raw.githubusercontent.com/pppaaasss/-/master/candidate/tv-core.m3u"
LIGHT_SAMPLE_BYTES = 2 * 1024 * 1024
DEEP_SAMPLE_BYTES = 12 * 1024 * 1024
SAMPLES_PER_ROUTE = 2
MIN_SAMPLE_BYTES = 64 * 1024
PLAYLIST_LIMIT = 1024 * 1024
HTTP_TIMEOUT = 10.0
FFPROBE_TIMEOUT = 18
STATUSES = {"GOOD", "DEGRADED", "UNKNOWN", "DEAD"}


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


def probe_route(name: str, url: str, *, floor: int, mode: str, config: dict) -> dict:
    result = empty_result(name, url, floor)
    limit = int(config.get("deep_sample_bytes") or DEEP_SAMPLE_BYTES) if mode == "deep" else int(
        config.get("light_sample_bytes") or LIGHT_SAMPLE_BYTES
    )
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

        if mode == "deep":
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


def selected_mode(requested: str, state: dict, now_epoch: float, config: dict) -> str:
    if requested in {"light", "deep"}:
        return requested
    previous = parse_utc(str(state.get("last_deep_utc") or ""))
    interval = float(config.get("deep_interval_hours") or 72) * 3600
    return "deep" if previous is None or now_epoch - previous >= interval else "light"


def minimum_height(name: str, config: dict) -> int:
    overrides = config.get("minimum_height_overrides") if isinstance(config.get("minimum_height_overrides"), dict) else {}
    return int(overrides.get(name) or (2160 if name == "CCTV-4K" else config.get("minimum_height_default") or 1080))


def run(config: dict, *, requested_mode: str = "auto", now_epoch: float | None = None) -> tuple[dict, dict]:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    started = time.monotonic()
    output_dir = Path(str(config.get("output_dir") or "/opt/var/lib/iptv-home-probe"))
    state_path = output_dir / "state.json"
    previous_state = load_json(state_path)
    mode = selected_mode(requested_mode, previous_state, now_epoch, config)
    resources = system_resources()
    blocked = resource_guard(resources, config)
    if blocked:
        raise RuntimeError(f"RESOURCE_GUARD:{blocked}")

    playlist_url = str(config.get("playlist_url") or DEFAULT_PLAYLIST)
    playlist_bytes, playlist_final, _ = fetch_playlist(playlist_url)
    entries = parse_playlist(playlist_bytes)
    maximum_runtime = float(config.get("maximum_runtime_s") or 3300)
    results: list[dict] = []
    for name, url in entries:
        if time.monotonic() - started >= maximum_runtime:
            raise RuntimeError("RUNTIME_BUDGET_EXHAUSTED")
        results.append(probe_route(name, url, floor=minimum_height(name, config), mode=mode, config=config))

    state, circuit = update_current_state(
        results,
        previous_state,
        now_epoch=now_epoch,
        mode=mode,
        config=config,
    )
    bad_names = {
        row["name"] for row in results
        if row.get("home_dead_confirmed") or row.get("home_degraded_confirmed")
    }
    candidate_results: list[dict] = []
    candidate_playlist = None
    if bad_names and config.get("candidate_playlist_url", DEFAULT_CANDIDATE_PLAYLIST):
        candidate_url = str(config.get("candidate_playlist_url") or DEFAULT_CANDIDATE_PLAYLIST)
        candidate_bytes, candidate_final, _ = fetch_playlist(candidate_url)
        candidate_entries = dict(parse_playlist(candidate_bytes))
        candidate_playlist = {
            "url": candidate_final,
            "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "channel_count": len(candidate_entries),
        }
        current_urls = {name: url for name, url in entries}
        for name in sorted(bad_names):
            url = str(candidate_entries.get(name) or "")
            if not url or url == current_urls.get(name):
                continue
            if time.monotonic() - started >= maximum_runtime:
                raise RuntimeError("RUNTIME_BUDGET_EXHAUSTED")
            candidate_results.append(
                probe_route(name, url, floor=minimum_height(name, config), mode="deep", config=config)
            )
        state = update_candidate_state(candidate_results, state, now_epoch=now_epoch, config=config)

    successful_runs = int(previous_state.get("successful_runs") or 0) + 1
    first_success = str(previous_state.get("first_success_utc") or "") or utc_text(now_epoch)
    state["successful_runs"] = successful_runs
    state["first_success_utc"] = first_success
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
        "circuit_breaker_open": bool(circuit),
    }
    report = {
        "schema": "iptv-home-probe/v1",
        "probe_id": str(config.get("probe_id") or "home-ac86u"),
        "generated_utc": utc_text(now_epoch),
        "run_status": "COMPLETED",
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
            "sample_bytes": int(config.get("deep_sample_bytes") or DEEP_SAMPLE_BYTES) if mode == "deep" else int(
                config.get("light_sample_bytes") or LIGHT_SAMPLE_BYTES
            ),
            "dead_after_runs": int(config.get("dead_after_runs") or 3),
            "dead_min_age_hours": float(config.get("dead_min_age_hours") or 6),
            "degraded_after_runs": int(config.get("degraded_after_runs") or 3),
            "degraded_min_age_hours": float(config.get("degraded_min_age_hours") or 6),
            "candidate_requires_two_runs": True,
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
    parser.add_argument("--mode", choices=("auto", "light", "deep"), default="auto")
    parser.add_argument("--now-epoch", type=float, default=None)
    args = parser.parse_args()
    try:
        config = load_json(Path(args.config))
        report, _ = run(config, requested_mode=args.mode, now_epoch=args.now_epoch)
        summary = report["summary"]
        print(
            "HOME_PROBE completed "
            f"mode={report['mode']} channels={summary['channels']} good={summary['good']} "
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
