#!/usr/bin/env python3
"""Resumable Hong Kong audit of every unique URL in harvest/candidates.jsonl.

This is a bulk research audit, not a production publisher. It deduplicates by
exact URL, probes each URL from the Hong Kong host, and checkpoints every
completed result so an SSH disconnect/reboot does not waste prior work.

Classification is intentionally generic because the same URL pool contains
core and non-core channels:
- GOOD: video decodes at >=720 and transport is usable
- DEGRADED: video decodes below 720, or obviously tiny H264 bitrate
- UNKNOWN: Hong Kong could not get reliable video/transport evidence

Per-channel publication floors (1080 for core, 2160 for CCTV-4K) remain a
separate later selection step. This program never modifies tv*.m3u.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import signal
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import hk_probe  # noqa: E402

DEFAULT_WORKERS = 4
MIN_GENERIC_HEIGHT = 720
LOW_H264_MBIT = 1.0


def load_harvest(path: Path) -> tuple[list[str], dict[str, dict]]:
    meta: dict[str, dict] = {}
    order: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        if url not in meta:
            meta[url] = {
                "url": url,
                "names": [],
                "groups": [],
                "sources": [],
            }
            order.append(url)
        m = meta[url]
        name = str(row.get("name") or "").strip()
        group = str(row.get("group") or "").strip()
        source = str(row.get("source") or "").strip()
        if name and name not in m["names"] and len(m["names"]) < 20:
            m["names"].append(name)
        if group and group not in m["groups"] and len(m["groups"]) < 20:
            m["groups"].append(group)
        if source and source not in m["sources"] and len(m["sources"]) < 20:
            m["sources"].append(source)
    return order, meta


def read_completed(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("url"):
                out[str(row["url"])] = row
    return out


def atomic_summary(out_dir: Path, total: int, done: dict[str, dict], started: str) -> None:
    counts = defaultdict(int)
    heights = defaultdict(int)
    for row in done.values():
        counts[str(row.get("status") or "UNKNOWN")] += 1
        h = int(row.get("height") or 0)
        if h >= 2160:
            heights["2160+"] += 1
        elif h >= 1080:
            heights["1080"] += 1
        elif h >= 720:
            heights["720"] += 1
        elif h > 0:
            heights["below720"] += 1
        else:
            heights["unknown"] += 1
    payload = {
        "started_utc": started,
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe_region": "Hong Kong",
        "stage": "all_harvested_unique_urls",
        "production_modified": False,
        "total_unique_urls": total,
        "completed": len(done),
        "remaining": max(0, total - len(done)),
        "percent": round((len(done) / total * 100.0) if total else 100.0, 3),
        "status_counts": dict(sorted(counts.items())),
        "height_buckets": dict(heights),
        "policy": {
            "checkpoint_each_result": True,
            "generic_good_floor": MIN_GENERIC_HEIGHT,
            "core_1080_floor_applied_later": True,
            "cctv4k_2160_floor_applied_later": True,
            "hk_network_failure_is_unknown_not_dead": True,
            "formal_playlists_modified": False,
        },
    }
    tmp = out_dir / "summary.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, out_dir / "summary.json")
    line = (
        f"TOTAL={total} DONE={len(done)} REMAINING={payload['remaining']} "
        f"GOOD={counts['GOOD']} DEGRADED={counts['DEGRADED']} UNKNOWN={counts['UNKNOWN']} "
        f"PROGRESS={payload['percent']:.2f}%"
    )
    (out_dir / "progress.txt").write_text(line + "\n", encoding="utf-8")


def probe_one(url: str, meta: dict) -> dict:
    started = time.monotonic()
    row = {
        "url": url,
        "names": list(meta.get("names") or []),
        "groups": list(meta.get("groups") or []),
        "sources": list(meta.get("sources") or []),
        "status": "UNKNOWN",
        "width": 0,
        "height": 0,
        "codec": "",
        "field_order": "",
        "fps": 0.0,
        "bitrate_mbps": 0.0,
        "segment_ok": False,
        "segment_mbps": 0.0,
        "startup_s": 0.0,
        "transport": "",
        "error": "",
        "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        info = hk_probe.ffprobe_meta(url)
        row.update(info)
    except Exception as exc:
        row["error"] = f"ffprobe:{type(exc).__name__}:{str(exc)[:220]}"
        row["elapsed_s"] = round(time.monotonic() - started, 3)
        return row

    # ffprobe successfully decoded video. Try a real HLS media-segment read as
    # stronger evidence. If this is not HLS, ffprobe success itself is accepted.
    ok, speed, startup_s, err = hk_probe.hls_segment_probe(url)
    row["segment_ok"] = bool(ok)
    row["segment_mbps"] = float(speed or 0)
    row["startup_s"] = float(startup_s or 0)
    if ok:
        row["transport"] = "hls_segment_verified"
    elif str(err).startswith("not_hls"):
        row["transport"] = "ffprobe_verified_non_hls"
    else:
        row["transport"] = "hls_segment_unverified"
        row["error"] = f"segment:{err}"
        row["elapsed_s"] = round(time.monotonic() - started, 3)
        return row

    height = int(row.get("height") or 0)
    codec = str(row.get("codec") or "").casefold()
    bitrate = float(row.get("bitrate_mbps") or 0)
    if height < MIN_GENERIC_HEIGHT:
        row["status"] = "DEGRADED"
        row["error"] = f"decoded_height_{height}_below_{MIN_GENERIC_HEIGHT}"
    elif codec == "h264" and 0 < bitrate < LOW_H264_MBIT:
        row["status"] = "DEGRADED"
        row["error"] = f"h264_intrinsic_bitrate_{bitrate:.3f}Mbps_too_low"
    else:
        row["status"] = "GOOD"
    row["elapsed_s"] = round(time.monotonic() - started, 3)
    return row


def export_final(out_dir: Path, order: list[str], completed: dict[str, dict]) -> None:
    for status in ("GOOD", "DEGRADED", "UNKNOWN"):
        path = out_dir / f"{status.lower()}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for url in order:
                row = completed.get(url)
                if row and row.get("status") == status:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    good = [completed[u] for u in order if u in completed and completed[u].get("status") == "GOOD"]
    good.sort(
        key=lambda r: (
            int(r.get("height") or 0),
            float(r.get("bitrate_mbps") or 0),
            float(r.get("segment_mbps") or 0),
        ),
        reverse=True,
    )
    m3u = ["#EXTM3U", "# Hong Kong full-harvest audit GOOD routes; research only"]
    for row in good:
        name = (row.get("names") or ["UNKNOWN"])[0].replace("\n", " ").replace(",", " ")
        m3u.append(
            f'#EXTINF:-1 hk-height="{int(row.get("height") or 0)}" hk-codec="{row.get("codec","")}" '
            f'hk-bitrate="{float(row.get("bitrate_mbps") or 0):.3f}",{name}'
        )
        m3u.append(str(row["url"]))
    (out_dir / "good.m3u").write_text("\n".join(m3u) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", default="harvest/candidates.jsonl")
    ap.add_argument("--output-dir", default="/var/lib/iptv-hk-probe/all-url-audit")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--fresh", action="store_true", help="discard previous checkpoint and start over")
    ap.add_argument("--limit", type=int, default=0, help="test helper: only audit first N unique URLs")
    args = ap.parse_args()

    harvest = Path(args.harvest)
    if not harvest.exists():
        print(f"harvest missing: {harvest}", file=sys.stderr)
        return 3
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "results.jsonl"
    if args.fresh:
        for p in out_dir.glob("*"):
            if p.is_file():
                p.unlink()

    order, meta = load_harvest(harvest)
    if args.limit > 0:
        order = order[: args.limit]
        meta = {u: meta[u] for u in order}
    completed = read_completed(result_path)
    # Drop stale checkpoint entries that are not in the current harvest view.
    completed = {u: row for u, row in completed.items() if u in meta}
    pending = [u for u in order if u not in completed]
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_summary(out_dir, len(order), completed, started)

    stop = {"requested": False}
    def request_stop(signum, frame):  # noqa: ARG001
        stop["requested"] = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    print(f"HK_ALL_URL_AUDIT total={len(order)} resumed={len(completed)} pending={len(pending)} workers={max(1,args.workers)}")
    sys.stdout.flush()

    with result_path.open("a", encoding="utf-8", buffering=1) as sink:
        # Small batches bound memory and make graceful interruption predictable.
        batch_size = max(8, max(1, args.workers) * 4)
        for offset in range(0, len(pending), batch_size):
            if stop["requested"]:
                break
            batch = pending[offset : offset + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
                futures = {ex.submit(probe_one, url, meta[url]): url for url in batch}
                for fut in concurrent.futures.as_completed(futures):
                    url = futures[fut]
                    try:
                        row = fut.result()
                    except Exception as exc:
                        row = {
                            "url": url,
                            "names": meta[url].get("names") or [],
                            "groups": meta[url].get("groups") or [],
                            "sources": meta[url].get("sources") or [],
                            "status": "UNKNOWN",
                            "error": f"worker:{type(exc).__name__}:{str(exc)[:220]}",
                            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        }
                    sink.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    sink.flush()
                    completed[url] = row
            atomic_summary(out_dir, len(order), completed, started)
            progress = (out_dir / "progress.txt").read_text(encoding="utf-8").strip()
            print(progress)
            sys.stdout.flush()

    atomic_summary(out_dir, len(order), completed, started)
    if len(completed) == len(order):
        export_final(out_dir, order, completed)
        print("HK_ALL_URL_AUDIT COMPLETE")
        return 0
    print("HK_ALL_URL_AUDIT PAUSED; rerun the same command to resume")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
