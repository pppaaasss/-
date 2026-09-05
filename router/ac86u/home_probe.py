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
import contextlib
import hashlib
import json
import os
import re
import socket
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .home_contract import (
        ContractError,
        REPORT_SCHEMA,
        ROUTE_CONTEXT,
        canonical_name,
        station_key,
        validate_backup_pool,
        validate_candidate_manifest,
        validate_home_report_v2,
    )
    from .home_decision import (
        backup_refresh_candidates,
        candidate_is_qualified,
        candidate_result,
        cached_backup_result,
        current_result,
        eligible_backups,
        mass_failure_circuit,
        probe_is_good,
        update_backup_pool,
        without_backup,
    )
except ImportError:  # Installed beside this file by the Entware installer.
    from home_contract import (  # type: ignore
        ContractError,
        REPORT_SCHEMA,
        ROUTE_CONTEXT,
        canonical_name,
        station_key,
        validate_backup_pool,
        validate_candidate_manifest,
        validate_home_report_v2,
    )
    from home_decision import (  # type: ignore
        backup_refresh_candidates,
        candidate_is_qualified,
        candidate_result,
        cached_backup_result,
        current_result,
        eligible_backups,
        mass_failure_circuit,
        probe_is_good,
        update_backup_pool,
        without_backup,
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
RUN_KINDS = {"primary-0200", "recheck-1300"}
_active_transport = None


@contextlib.contextmanager
def transport_context(config):
    global _active_transport
    if config.get("runtime_transport") != "merlinclash-marked":
        yield None
        return
    try:
        from .home_transport import HomeTransport
    except ImportError:
        from home_transport import HomeTransport
    if _active_transport is not None:
        raise RuntimeError("nested home transport unsupported")
    with HomeTransport(config.get("lan_dns_server", "192.168.50.1")) as transport:
        _active_transport = transport
        try:
            yield transport
        finally:
            _active_transport = None


def open_request(request):
    try:
        opener = _active_transport.opener.open if _active_transport else urllib.request.urlopen
        return opener(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if _active_transport and exc.headers.get("X-IPTV-Transport-Error"):
            exc.close()
            raise RuntimeError("local_transport_failure") from exc
        raise
    except urllib.error.URLError as exc:
        if _active_transport:
            raise RuntimeError("transport_connect_unknown:" + str(exc.reason)) from exc
        raise


def utc_text(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else float(epoch)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


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
    with open_request(request) as response:
        # read1 returns available bytes instead of waiting for an entire large
        # sample. Stop slow/trickling streams at a wall-clock transfer budget.
        chunks = []
        size = 0
        reader = getattr(response, "read1", response.read)
        while size < limit:
            if time.monotonic() - started >= HTTP_TIMEOUT:
                raise TimeoutError("stream_sample_transfer_timeout")
            chunk = reader(min(64 * 1024, limit - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
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
    return sorted(
        output,
        key=lambda row: (
            int(row["_queue_priority"]) if "_queue_priority" in row else 1,
            str(row.get("_expires_utc") or "9999"),
            str(row.get("channel_key")),
            str(row.get("candidate_id")),
        ),
    )


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
    options = {}
    if _active_transport:
        command[-1:-1] = ["-http_proxy", _active_transport.url]
        options["env"] = _active_transport.child_env()
    process = subprocess.run(command, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT, **options)
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
                viewer_accepted = bool(config.get("viewer_accepted_quality"))
                if result["height"] < floor and not viewer_accepted:
                    result["observed_status"] = "DEGRADED"
                    result["error"] = f"decoded_height_{result['height']}_below_{floor}"
                intrinsic = result["stream_mbps"] or result["bitrate_mbps"]
                codec = result["codec"].casefold()
                if codec == "h264":
                    minimum_stream = float(config.get("minimum_h264_stream_mbps") or 5.0)
                elif codec in {"h265", "hevc"}:
                    minimum_stream = float(config.get("minimum_hevc_stream_mbps") or 2.5)
                else:
                    minimum_stream = float(config.get("minimum_other_stream_mbps") or 3.0)
                if intrinsic <= 0:
                    result["observed_status"] = "UNKNOWN"
                    result["error"] = "intrinsic_stream_bitrate_unknown"
                elif intrinsic < minimum_stream and not viewer_accepted:
                    result["observed_status"] = "DEGRADED"
                    result["error"] = (
                        f"{codec or 'unknown'}_stream_{intrinsic:.3f}_below_{minimum_stream:.3f}_mbps"
                    )
            except Exception as exc:
                # Segment evidence remains useful.  Missing ffprobe metadata is
                # quality-unknown, not proof that a playing route is dead.
                result["error"] = (result["error"] + ";" if result["error"] else "") + (
                    f"quality_unknown:{type(exc).__name__}:{str(exc)[:180]}"
                )
        result["status"] = result["observed_status"]
    except Exception as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        explicit_failure = (
            isinstance(exc, urllib.error.HTTPError) and exc.code in {404, 410, 500, 502, 503, 504}
        ) or isinstance(reason, (TimeoutError, ConnectionError, socket.timeout))
        result["observed_status"] = "UNAVAILABLE" if explicit_failure else "UNKNOWN"
        result["status"] = result["observed_status"]
        result["error"] = f"{type(exc).__name__}:{str(exc)[:240]}"
    result["probe_runtime_s"] = round(time.monotonic() - started, 3)
    return result


def minimum_height(name: str, config: dict) -> int:
    overrides = config.get("minimum_height_overrides") if isinstance(config.get("minimum_height_overrides"), dict) else {}
    return int(overrides.get(name) or (2160 if name == "CCTV-4K" else config.get("minimum_height_default") or 1080))


def _runtime_unknown(name: str, url: str, channel_key: str, floor: int) -> dict:
    row = empty_result(name, url, floor)
    row["channel_key"] = channel_key
    row["error"] = "runtime_budget_exhausted_before_attempt"
    return row


def _probe_current(
    name: str,
    url: str,
    channel_key: str,
    *,
    profile: dict,
    config: dict,
) -> dict:
    current_config = dict(config)
    feedback = config.get("home_feedback") or {}
    current_config["viewer_accepted_quality"] = url in feedback_urls(feedback, "good")
    row = probe_route(
        name,
        url,
        floor=minimum_height(name, config),
        config=current_config,
        sample_limit=int(profile["current_sample_bytes"]),
        include_metadata=bool(profile["current_metadata"]),
    )
    row["channel_key"] = channel_key
    if url in feedback_urls(feedback, "bad"):
        row["observed_status"] = row["status"] = "DEGRADED"
        row["error"] = "viewer_confirmed_bad_route"
    return row


def feedback_urls(feedback: dict, category: str) -> set[str]:
    return {
        str(item.get("url") if isinstance(item, dict) else item).strip()
        for rows in (feedback.get(category) or {}).values()
        for item in rows
    }


def load_home_feedback(config: dict, output_dir: Path) -> tuple[dict, str, str]:
    url = str(config.get("home_feedback_url") or "")
    if not url:
        return dict(config.get("home_feedback") or {}), "not_configured", ""
    cache = output_dir / "home-feedback.json"
    try:
        raw, _final, _elapsed = fetch_playlist(url)
        value = json.loads(raw)
        if not isinstance(value, dict) or any(not isinstance(value.get(k, {}), dict) for k in ("good", "bad")):
            raise ValueError("invalid home feedback")
        feedback_urls(value, "bad")
        feedback_urls(value, "good")
        atomic_json(cache, value)
        return value, "fresh", hashlib.sha256(raw).hexdigest()
    except Exception:
        return load_json(cache), "unavailable", ""


def _valid_sha(value: object) -> str:
    text = str(value or "").lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else hashlib.sha256(b"").hexdigest()


def _run(
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
    backup_path = output_dir / "qualified-backups.json"
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
    resources = system_resources()
    blocked = resource_guard(resources, config)
    if blocked:
        raise RuntimeError(f"RESOURCE_GUARD:{blocked}")
    timezone_guard(config, now_epoch)
    route_context = str(config.get("route_context") or ROUTE_CONTEXT)
    if route_context != ROUTE_CONTEXT:
        raise RuntimeError("ROUTE_CONTEXT_UNVERIFIED")

    playlist_url = str(config.get("playlist_url") or DEFAULT_PLAYLIST)
    playlist_bytes, playlist_final, _ = fetch_playlist(playlist_url)
    entries = parse_playlist(playlist_bytes)
    maximum_runtime = min(float(config.get("maximum_runtime_s") or 1200), 1200)
    config = dict(config)
    feedback, feedback_state, feedback_sha = load_home_feedback(config, output_dir)
    config["home_feedback"] = feedback
    veto_urls = feedback_urls(feedback, "bad")
    formal_rows: list[tuple[str, str, str]] = []
    formal_keys: set[str] = set()
    for playlist_name, url in entries:
        key = station_key(playlist_name)
        if key is None or key in formal_keys:
            raise RuntimeError(f"FORMAL_PLAYLIST_SCOPE_OR_DUPLICATE:{playlist_name}")
        formal_keys.add(key)
        formal_rows.append((key, canonical_name(key), url))

    # Every formal route gets its first attempt before any retry or candidate
    # work.  This makes the current household picture the run's top priority.
    attempts_by_key: dict[str, list[dict]] = {}
    for key, name, url in formal_rows:
        if time.monotonic() - started >= maximum_runtime:
            attempts_by_key[key] = [_runtime_unknown(name, url, key, minimum_height(name, config))]
            continue
        attempts_by_key[key] = [_probe_current(name, url, key, profile=profile, config=config)]

    # A non-GOOD first attempt is never enough to replace a route.  Confirm it
    # once more in the same home run; no arbitrary channel-count limit applies.
    for key, name, url in formal_rows:
        attempts = attempts_by_key[key]
        if probe_is_good(attempts[0]) or time.monotonic() - started >= maximum_runtime:
            continue
        attempts.append(_probe_current(name, url, key, profile=profile, config=config))

    circuit = mass_failure_circuit(
        attempts_by_key,
        minimum_channels=int(config.get("circuit_breaker_min_unknown") or 12),
        failure_ratio=float(config.get("circuit_breaker_unknown_ratio") or 0.35),
    )
    current_results = [
        current_result(name, url, attempts_by_key[key], circuit_open=circuit)
        for key, name, url in formal_rows
    ]
    current_urls = {key: url for key, _name, url in formal_rows}

    state = dict(previous_state)
    state.update({
        "version": 2,
        "last_run_utc": utc_text(now_epoch),
        "last_run_kind": run_kind,
        "circuit_breaker_open": bool(circuit),
        "current": {row["channel_key"]: row for row in current_results},
    })

    existing_pool: dict | None
    try:
        existing_pool = load_json(backup_path) if backup_path.exists() else None
        if existing_pool is not None:
            validate_backup_pool(
                existing_pool,
                expected_probe_id=str(config.get("probe_id") or "home-ac86u"),
                now_epoch=now_epoch,
                allow_expired=True,
            )
    except Exception:
        existing_pool = None
        state["backup_pool_load_error"] = True
    existing_manifest_sha = _valid_sha(
        (existing_pool or {}).get("candidate_manifest_sha256")
        or previous_state.get("last_candidate_manifest_sha256")
    )
    candidate_manifest_sha = existing_manifest_sha
    candidate_report_by_id: dict[str, dict] = {}
    newly_qualified: list[tuple[dict, dict]] = []
    candidate_playlist = None
    candidate_manifest_state = "not_requested"
    actionable = bool(config.get("actionable", False)) and feedback_state != "unavailable"
    choices: dict[str, str] = {}
    attempted_keys: set[str] = set()
    attempted_ids: set[str] = set()
    budget_keys: set[str] = set()
    bad_keys = {str(row["channel_key"]) for row in current_results if row["status"] == "BAD"}
    if existing_pool:
        for backup in list(existing_pool["backups"]):
            if backup["url"] in veto_urls or backup.get("request_options"):
                existing_pool = without_backup(existing_pool, backup["candidate_id"])

    # Repair evidence comes first. 13:00 only reads existing primary evidence;
    # it never requests a backup URL, even when a replacement is needed.
    if not circuit:
        for key in sorted(bad_keys):
            for backup in eligible_backups(existing_pool, key, now_epoch=now_epoch):
                identity = str(backup["candidate_id"])
                if run_kind == "recheck-1300":
                    if backup.get("verified_run_kind") != "primary-0200":
                        continue
                    candidate_report_by_id[identity] = cached_backup_result(backup)
                    choices[key] = identity
                    break
                if time.monotonic() - started >= maximum_runtime:
                    budget_keys.add(key)
                    break
                attempted_keys.add(key)
                attempted_ids.add(identity)
                raw = probe_route(
                    canonical_name(key), backup["url"], floor=minimum_height(canonical_name(key), config),
                    config=config, sample_limit=int(profile["candidate_sample_bytes"]), include_metadata=True,
                )
                raw["channel_key"] = key
                evidence = candidate_result(backup, raw, purpose="switch-reverification", switch_reverified=True)
                candidate_report_by_id[identity] = evidence
                if candidate_is_qualified(raw):
                    choices[key] = identity
                    newly_qualified.append((backup, raw))
                    break
                existing_pool = without_backup(existing_pool, identity)

    if profile["scan_candidates"]:
        incoming = backup_refresh_candidates(
            existing_pool,
            now_epoch=now_epoch,
            refresh_before_hours=float(config.get("backup_refresh_before_hours") or 18),
        )
        candidate_url = str(config.get("candidate_manifest_url", DEFAULT_CANDIDATE_MANIFEST)).strip()
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
                candidate_manifest_sha = candidate_playlist["sha256"]
                for value in manifest["candidates"]:
                    candidate = dict(value)
                    candidate["_queue_priority"] = 1
                    candidate["source_manifest_sha256"] = candidate_manifest_sha
                    incoming.append(candidate)
                state["last_candidate_manifest_sha256"] = candidate_manifest_sha
                state["last_candidate_manifest_utc"] = str(manifest["generated_utc"])
                candidate_manifest_state = "accepted"
            except Exception as exc:
                # A stale, racing, or malformed cloud manifest must never make
                # the formal home-health pass or local backup refresh fail.
                candidate_manifest_state = f"rejected:{type(exc).__name__}:{str(exc)[:220]}"

        queue = merge_candidate_queue(
            previous_state.get("candidate_queue"),
            incoming,
            current_urls,
        )
        rounds = {}
        per_channel = {}
        for candidate in queue:
            key = candidate["channel_key"]
            rounds[candidate["candidate_id"]] = per_channel.get(key, 0)
            per_channel[key] = per_channel.get(key, 0) + 1
        queue.sort(key=lambda row: (
            rounds[row["candidate_id"]],
            0 if row["channel_key"] in bad_keys and row["channel_key"] not in choices else 1,
            len(eligible_backups(existing_pool, row["channel_key"], now_epoch=now_epoch)),
            int(row.get("_queue_priority", 1)),
            str(row.get("_expires_utc", "9999")),
            str(row["channel_key"]), str(row["candidate_id"]),
        ))
        remaining: list[dict] = []
        observations = dict(previous_state.get("candidate_observations") or {}) if isinstance(
            previous_state.get("candidate_observations"), dict
        ) else {}
        max_unknown_retries = max(1, int(config.get("candidate_unknown_retry_runs") or 2))
        for candidate in queue:
            if candidate["candidate_id"] in attempted_ids or candidate["url"] in veto_urls or candidate.get("request_options"):
                continue
            if circuit or time.monotonic() - started >= maximum_runtime:
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
            row["channel_key"] = key
            report_row = candidate_result(
                candidate,
                row,
                purpose="daily-qualification",
                switch_reverified=False,
            )
            identity = str(candidate["candidate_id"])
            candidate_report_by_id[identity] = report_row
            if candidate_is_qualified(row):
                candidate_with_source = dict(candidate)
                candidate_with_source["source_manifest_sha256"] = str(
                    candidate.get("source_manifest_sha256") or candidate_manifest_sha
                )
                newly_qualified.append((candidate_with_source, row))
                if key in bad_keys and key not in choices:
                    # This full home check was performed after the current
                    # failure in this same primary run; use it immediately.
                    choices[key] = identity
                    candidate_report_by_id[identity] = candidate_result(
                        candidate, row, purpose="switch-reverification", switch_reverified=True,
                    )
            elif existing_pool:
                existing_pool = without_backup(existing_pool, identity)
            old_observation = observations.get(identity) if isinstance(observations.get(identity), dict) else {}
            unknown_attempts = int(old_observation.get("unknown_attempts") or 0)
            qualification = str(report_row["qualification"])
            unknown_attempts = unknown_attempts + 1 if qualification == "UNKNOWN" else 0
            observations[identity] = {
                "candidate": candidate,
                "qualification": qualification,
                "unknown_attempts": unknown_attempts,
                "last_checked_utc": utc_text(now_epoch),
                "result": report_row,
            }
            if qualification == "UNKNOWN" and unknown_attempts < max_unknown_retries:
                remaining.append(candidate)
        state["candidate_queue"] = remaining
        state["candidate_observations"] = observations

    pool = update_backup_pool(
        existing_pool,
        newly_qualified,
        probe_id=str(config.get("probe_id") or "home-ac86u"),
        now_epoch=now_epoch,
        formal_playlist_sha256=hashlib.sha256(playlist_bytes).hexdigest(),
        candidate_manifest_sha256=candidate_manifest_sha,
        current_urls=current_urls,
        ttl_hours=float(config.get("qualified_backup_ttl_hours") or 36),
    )

    decisions: list[dict] = []
    for row in current_results:
        key = str(row["channel_key"])
        status = str(row["status"])
        if status == "GOOD":
            decisions.append({
                "channel_key": key,
                "action": "KEEP",
                "reason": "healthy_home_route",
                "replacement_candidate_id": None,
            })
            continue
        if status == "UNKNOWN":
            decisions.append({
                "channel_key": key,
                "action": "UNRESOLVED",
                "reason": "insufficient_home_evidence",
                "replacement_candidate_id": None,
            })
            continue
        if not actionable:
            decisions.append({
                "channel_key": key,
                "action": "UNRESOLVED",
                "reason": "shadow_mode",
                "replacement_candidate_id": None,
            })
            continue

        replacement = choices.get(key)
        reverify_attempted = key in attempted_keys
        budget_exhausted = key in budget_keys
        if replacement:
            decisions.append({
                "channel_key": key,
                "action": "REPLACE",
                "reason": "confirmed_home_failure_and_cached_primary_backup" if run_kind == "recheck-1300" else "confirmed_home_failure_and_reverified_backup",
                "replacement_candidate_id": replacement,
            })
        else:
            reason = "runtime_budget_before_reverification" if budget_exhausted else (
                "home_backups_failed_reverification" if reverify_attempted else "no_home_qualified_backup"
            )
            decisions.append({
                "channel_key": key,
                "action": "UNRESOLVED",
                "reason": reason,
                "replacement_candidate_id": None,
            })

    successful_runs = int(previous_state.get("successful_runs") or 0) + 1
    first_success = str(previous_state.get("first_success_utc") or "") or utc_text(now_epoch)
    state["successful_runs"] = successful_runs
    state["first_success_utc"] = first_success
    state["last_run_kind"] = run_kind
    state["candidate_manifest_state"] = candidate_manifest_state
    state["qualified_backup_pool"] = pool
    runtime = round(time.monotonic() - started, 3)
    resources["runtime_s"] = runtime
    candidate_results = sorted(candidate_report_by_id.values(), key=lambda row: str(row["candidate_id"]))
    summary = {
        "channels": len(current_results),
        "good": sum(row["status"] == "GOOD" for row in current_results),
        "bad": sum(row["status"] == "BAD" for row in current_results),
        "unknown": sum(row["status"] == "UNKNOWN" for row in current_results),
        "candidate_channels": len(candidate_results),
        "candidate_confirmed": sum(row.get("qualification") == "QUALIFIED" for row in candidate_results),
        "candidate_queue_remaining": len(state.get("candidate_queue") or []),
        "qualified_backups": int(pool["backup_count"]),
        "replacements": sum(row["action"] == "REPLACE" for row in decisions),
        "unresolved": sum(row["action"] == "UNRESOLVED" for row in decisions),
        "circuit_breaker_open": bool(circuit),
    }
    report = {
        "schema": REPORT_SCHEMA,
        "probe_id": str(config.get("probe_id") or "home-ac86u"),
        "generated_utc": utc_text(now_epoch),
        "run_status": "COMPLETED",
        "run_kind": run_kind,
        "actionable": actionable,
        "production_modified": False,
        "route_context": route_context,
        "formal_playlist": {
            "url": playlist_final,
            "sha256": hashlib.sha256(playlist_bytes).hexdigest(),
            "channel_count": len(formal_rows),
        },
        "baseline": {
            "home_network_ok": not circuit,
            "github_reachable": True,
            "route_verified": True,
            "mass_failure_circuit_breaker": bool(circuit),
        },
        "candidate_playlist": candidate_playlist,
        "home_feedback_sha256": feedback_sha,
        "policy": {
            "auto_replace_formal_routes": False,
            "mass_failure_circuit_breaker": True,
            "single_threaded": True,
            "samples_per_route": SAMPLES_PER_ROUTE,
            "sample_bytes": int(profile["current_sample_bytes"]),
            "candidate_sample_bytes": int(profile["candidate_sample_bytes"]),
            "candidate_manifest_state": candidate_manifest_state,
            "home_feedback_state": feedback_state,
            "afternoon_backup_probes": False,
            "current_failure_requires_two_attempts": True,
            "candidate_requires_two_samples_and_deep_metadata": True,
            "qualified_backup_ttl_hours": float(config.get("qualified_backup_ttl_hours") or 36),
        },
        "resources": resources,
        "summary": summary,
        "current_results": current_results,
        "candidate_results": candidate_results,
        "decisions": decisions,
    }
    validate_backup_pool(
        pool,
        expected_probe_id=str(config.get("probe_id") or "home-ac86u"),
        now_epoch=now_epoch,
    )
    validate_home_report_v2(
        report,
        expected_probe_id=str(config.get("probe_id") or "home-ac86u"),
        now_epoch=now_epoch,
    )
    atomic_json(backup_path, pool)
    atomic_json(output_dir / "latest.json", report)
    atomic_json(state_path, state)
    return report, state


def run(config, **kwargs):
    with transport_context(config):
        return _run(config, **kwargs)


def main() -> int:
    def stop(_signum, _frame):
        raise InterruptedError("home probe interrupted")
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
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
            f"run_kind={report['run_kind']} channels={summary['channels']} good={summary['good']} "
            f"bad={summary['bad']} unknown={summary['unknown']} backups={summary['qualified_backups']} "
            f"replacements={summary['replacements']} candidates={summary['candidate_confirmed']} "
            f"circuit={int(summary['circuit_breaker_open'])}"
        )
        return 0
    except Exception as exc:
        message = str(exc)
        print(f"HOME_PROBE skipped_or_failed: {message}", file=sys.stderr)
        return 75 if message.startswith("RESOURCE_GUARD:") else 2


if __name__ == "__main__":
    raise SystemExit(main())
