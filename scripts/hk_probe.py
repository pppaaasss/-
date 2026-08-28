#!/usr/bin/env python3
"""Hong Kong probe for formal IPTV playlists.

This agent is intentionally read-only with respect to production playlists.
It can probe the full formal tv.m3u while applying stricter quality floors to
core CCTV/satellite channels.

Policy:
- core decoded resolution below 1080 is DEGRADED; CCTV-4K floor is 2160
- non-core formal channels may use a lower configured floor (normally 720)
- one or two Hong Kong failures are UNKNOWN, never "dead"
- the same URL needs three failed runs spanning at least six hours to be DEAD
- a mass-failure run opens the circuit breaker and does not advance counters
- HLS segment failure with otherwise valid decode is UNKNOWN
- production playlists are never modified by this program
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (HK-IPTV-Probe/1.1)"
HTTP_TIMEOUT = 8.0
FFPROBE_TIMEOUT = 16
WORKERS = 8
SEGMENT_READ = 512 * 1024


@dataclass
class ProbeResult:
    name: str
    url: str
    status: str = "UNKNOWN"
    width: int = 0
    height: int = 0
    codec: str = ""
    field_order: str = ""
    fps: float = 0.0
    bitrate_mbps: float = 0.0
    segment_ok: bool = False
    segment_mbps: float = 0.0
    startup_s: float = 0.0
    min_height: int = 1080
    error: str = ""
    observed_status: str = ""
    consecutive_failures: int = 0
    failure_age_hours: float = 0.0
    hk_dead_confirmed: bool = False


def parse_rate(raw: str | None) -> float:
    if not raw or raw in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in raw:
            a, b = raw.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(raw)
    except Exception:
        return 0.0


def load_playlist(path: Path) -> list[tuple[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF:") or "," not in line:
            continue
        name = line.rsplit(",", 1)[-1].strip()
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
            j += 1
        if j < len(lines):
            url = lines[j].strip()
            if url.startswith(("http://", "https://")):
                rows.append((name, url))
    return rows


def ffprobe_meta(url: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-rw_timeout", "8000000",
        "-probesize", "3000000",
        "-analyzeduration", "5000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,field_order,avg_frame_rate,r_frame_rate,bit_rate",
        "-show_entries", "format=bit_rate",
        "-of", "json", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe_failed").strip()[-240:])
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("no_video_stream")
    s = streams[0]
    bitrate = 0.0
    for raw in (s.get("bit_rate"), (payload.get("format") or {}).get("bit_rate")):
        try:
            bitrate = max(bitrate, float(raw or 0) / 1_000_000)
        except Exception:
            pass
    return {
        "width": int(s.get("width") or 0),
        "height": int(s.get("height") or 0),
        "codec": str(s.get("codec_name") or ""),
        "field_order": str(s.get("field_order") or ""),
        "fps": max(parse_rate(s.get("avg_frame_rate")), parse_rate(s.get("r_frame_rate"))),
        "bitrate_mbps": round(bitrate, 3),
    }


def fetch_bytes(url: str, limit: int) -> tuple[bytes, str, float]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read(limit)
        elapsed = max(time.monotonic() - started, 0.001)
        return data, resp.geturl(), elapsed


def fetch_text(url: str, limit: int = 512 * 1024) -> tuple[str, str, float]:
    data, final, elapsed = fetch_bytes(url, limit)
    return data.decode("utf-8", "ignore"), final, elapsed


def choose_variant(text: str, base: str) -> str | None:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    variants: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        rm = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bm = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
        h = int(rm.group(2)) if rm else 0
        bw = int(bm.group(1)) if bm else 0
        for nxt in lines[i + 1 :]:
            if nxt and not nxt.startswith("#"):
                variants.append((h, bw, urllib.parse.urljoin(base, nxt)))
                break
    if not variants:
        return None
    return max(variants, key=lambda x: (x[0], x[1]))[2]


def newest_segment(text: str, base: str) -> str | None:
    last = None
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            last = urllib.parse.urljoin(base, line)
    return last


def hls_segment_probe(url: str) -> tuple[bool, float, float, str]:
    try:
        text, final, manifest_s = fetch_text(url)
        if "#EXTM3U" not in text:
            return False, 0.0, manifest_s, "not_hls"
        child = choose_variant(text, final)
        if child:
            text, final, child_s = fetch_text(child)
            manifest_s += child_s
            if "#EXTM3U" not in text:
                return False, 0.0, manifest_s, "bad_child_playlist"
        segment = newest_segment(text, final)
        if not segment:
            return False, 0.0, manifest_s, "no_segment"
        data, _, seg_s = fetch_bytes(segment, SEGMENT_READ)
        if len(data) < 32 * 1024:
            return False, 0.0, manifest_s + seg_s, "short_segment"
        mbps = len(data) * 8 / max(seg_s, 0.001) / 1_000_000
        return True, round(mbps, 3), round(manifest_s + seg_s, 3), ""
    except Exception as exc:
        return False, 0.0, 0.0, f"{type(exc).__name__}:{str(exc)[:180]}"


def probe_one(item) -> ProbeResult:
    if len(item) == 3:
        name, url, floor = item
    else:
        name, url = item
        floor = 2160 if name == "CCTV-4K" else 1080
    r = ProbeResult(name=name, url=url, min_height=int(floor))
    try:
        meta = ffprobe_meta(url)
        r.width = meta["width"]
        r.height = meta["height"]
        r.codec = meta["codec"]
        r.field_order = meta["field_order"]
        r.fps = round(meta["fps"], 3)
        r.bitrate_mbps = meta["bitrate_mbps"]
    except Exception as exc:
        r.status = "UNKNOWN"
        r.error = f"ffprobe:{type(exc).__name__}:{str(exc)[:180]}"
        return r

    if r.height < r.min_height:
        r.status = "DEGRADED"
        r.error = f"decoded_height_{r.height}_below_{r.min_height}"
        return r

    ok, speed, startup, err = hls_segment_probe(url)
    r.segment_ok = ok
    r.segment_mbps = speed
    r.startup_s = startup
    if not ok:
        r.status = "UNKNOWN"
        r.error = f"segment:{err}"
        return r

    r.status = "GOOD"
    return r


def parse_utc(raw: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def utc_text(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def update_state(
    results: list[ProbeResult],
    state_path: Path,
    *,
    now_epoch: float | None = None,
    dead_after_runs: int = 3,
    dead_min_age_hours: float = 6.0,
    circuit_breaker_unknown_ratio: float = 0.25,
    circuit_breaker_min_unknown: int = 20,
) -> dict:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    now = utc_text(now_epoch)
    old = {}
    if state_path.exists():
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    channels = old.get("channels") if isinstance(old, dict) else {}
    if not isinstance(channels, dict):
        channels = {}

    unknown_count = sum(r.status == "UNKNOWN" for r in results)
    unknown_ratio = unknown_count / len(results) if results else 0.0
    circuit_open = (
        unknown_count >= max(1, int(circuit_breaker_min_unknown))
        and unknown_ratio >= float(circuit_breaker_unknown_ratio)
    )

    alerts = []
    for r in results:
        prev = channels.get(r.name, {}) if isinstance(channels.get(r.name), dict) else {}
        observed = r.status
        r.observed_status = observed
        same_url = str(prev.get("last_url") or "") == r.url
        previous_failures = int(prev.get("consecutive_failures", 0)) if same_url else 0
        first_failure = str(prev.get("first_failure_utc") or "") if same_url else ""
        dead_since = str(prev.get("dead_since_utc") or "") if same_url else ""

        if observed in {"GOOD", "DEGRADED"}:
            effective = observed
            consecutive = 0
            first_failure = ""
            dead_since = ""
            failure_age_hours = 0.0
        else:
            if circuit_open:
                consecutive = previous_failures
            else:
                consecutive = previous_failures + 1
                if not first_failure:
                    first_failure = now
            first_epoch = parse_utc(first_failure) if first_failure else None
            failure_age_hours = round(max(0.0, now_epoch - first_epoch) / 3600, 3) if first_epoch else 0.0
            was_dead = same_url and str(prev.get("status") or "") == "DEAD"
            newly_dead = (
                not circuit_open
                and consecutive >= max(1, int(dead_after_runs))
                and failure_age_hours >= max(0.0, float(dead_min_age_hours))
            )
            effective = "DEAD" if was_dead or newly_dead else "UNKNOWN"
            if effective == "DEAD" and not dead_since:
                dead_since = now

        r.status = effective
        r.consecutive_failures = consecutive
        r.failure_age_hours = failure_age_hours
        r.hk_dead_confirmed = effective == "DEAD"
        channels[r.name] = {
            "status": effective,
            "observed_status": observed,
            "consecutive_failures": consecutive,
            "first_failure_utc": first_failure,
            "last_failure_utc": now if observed == "UNKNOWN" else "",
            "dead_since_utc": dead_since,
            "last_url": r.url,
            "last_height": r.height,
            "min_height": r.min_height,
            "updated_utc": now,
        }
        if effective == "DEAD":
            alerts.append({
                "name": r.name,
                "reason": r.error,
                "url": r.url,
                "consecutive_failures": consecutive,
                "failure_age_hours": failure_age_hours,
                "secondary_confirmation_required": True,
                "replacement_eligible": False,
            })

    state = {
        "version": 3,
        "policy": {
            "dead_after_runs": int(dead_after_runs),
            "dead_min_age_hours": float(dead_min_age_hours),
            "degraded_is_alive": True,
            "secondary_confirmation_required": True,
            "automatic_replacement_enabled": False,
        },
        "last_run": {
            "generated_utc": now,
            "channels": len(results),
            "unknown_observations": unknown_count,
            "unknown_ratio": round(unknown_ratio, 4),
            "circuit_breaker_open": circuit_open,
        },
        "channels": channels,
        "alerts": alerts,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)
    return state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default="tv-core.m3u")
    ap.add_argument("--core-playlist", default="")
    ap.add_argument("--default-min-height", type=int, default=1080)
    ap.add_argument("--output-dir", default="/var/lib/iptv-hk-probe")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--dead-after-runs", type=int, default=3)
    ap.add_argument("--dead-min-age-hours", type=float, default=6.0)
    ap.add_argument("--circuit-breaker-unknown-ratio", type=float, default=0.25)
    ap.add_argument("--circuit-breaker-min-unknown", type=int, default=20)
    args = ap.parse_args()

    playlist = Path(args.playlist)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = load_playlist(playlist)
    core_names = set()
    if args.core_playlist:
        core_names = {name for name, _ in load_playlist(Path(args.core_playlist))}
    items = []
    for name, url in rows:
        if name == "CCTV-4K":
            floor = 2160
        elif name in core_names:
            floor = 1080
        else:
            floor = args.default_min_height
        items.append((name, url, floor))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        results = list(ex.map(probe_one, items))

    results.sort(key=lambda x: x.name)
    state = update_state(
        results,
        out / "state.json",
        dead_after_runs=args.dead_after_runs,
        dead_min_age_hours=args.dead_min_age_hours,
        circuit_breaker_unknown_ratio=args.circuit_breaker_unknown_ratio,
        circuit_breaker_min_unknown=args.circuit_breaker_min_unknown,
    )
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe_region": "Hong Kong",
        "playlist": str(playlist),
        "core_playlist": args.core_playlist,
        "production_modified": False,
        "policy": {
            "core_height_floor_1080": True,
            "cctv4k_height_floor_2160": True,
            "noncore_default_height_floor": args.default_min_height,
            "single_hk_failure_is_unknown": True,
            "degraded_stream_is_alive": True,
            "dead_after_consecutive_unknown_runs": args.dead_after_runs,
            "dead_min_failure_age_hours": args.dead_min_age_hours,
            "mass_failure_circuit_breaker": True,
            "dead_requires_secondary_confirmation_before_replacement": True,
            "auto_replace_formal_routes": False,
        },
        "summary": {
            "channels": len(results),
            "good": sum(r.status == "GOOD" for r in results),
            "degraded": sum(r.status == "DEGRADED" for r in results),
            "unknown": sum(r.status == "UNKNOWN" for r in results),
            "dead": sum(r.status == "DEAD" for r in results),
            "dead_alerts": len(state.get("alerts") or []),
            "circuit_breaker_open": bool((state.get("last_run") or {}).get("circuit_breaker_open")),
        },
        "results": [asdict(r) for r in results],
        "alerts": state.get("alerts") or [],
    }
    (out / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    text = [
        f"generated_utc={payload['generated_utc']}",
        f"CHANNELS={payload['summary']['channels']} GOOD={payload['summary']['good']} DEGRADED={payload['summary']['degraded']} UNKNOWN={payload['summary']['unknown']} DEAD={payload['summary']['dead']} CIRCUIT={int(payload['summary']['circuit_breaker_open'])}",
    ]
    for r in results:
        dims = f"{r.width}x{r.height}" if r.width and r.height else "-"
        text.append(f"{r.status:8} {r.name:18} {dims:10} {r.codec:6} floor={r.min_height} seg={r.segment_mbps:.2f}Mbps {r.error}")
    if payload["alerts"]:
        text.append("ALERTS:")
        for a in payload["alerts"]:
            text.append(f"  {a['name']} consecutive={a['consecutive_failures']} age={a['failure_age_hours']:.1f}h {a['reason']}")
    (out / "latest.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    print("\n".join(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
