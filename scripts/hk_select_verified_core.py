#!/usr/bin/env python3
"""Quality-aware core spare selection from the fully audited Hong Kong pool.

Unlike the legacy filter, this program never truncates raw/unmeasured candidates.
It expects harvest/candidates.jsonl to contain GOOD-only routes enriched with
``hk_verified`` evidence from the full 17k Hong Kong audit.  It first filters
all matched routes by the formal core quality floor, ranks them using the full
audit evidence, then fresh-probes only a few diverse top candidates.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.parse
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import hk_probe  # noqa: E402
import hk_filter_harvest as legacy  # noqa: E402


def load_rows(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if isinstance(row, dict) and str(row.get("url") or "").startswith(("http://", "https://")):
            out.append(row)
    return out


def load_bad_urls(path: Path) -> set[str]:
    return legacy.load_bad_urls(path)


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").casefold()
    except Exception:
        return ""


def floor_for(target: str) -> int:
    return 2160 if target == "CCTV-4K" else 1080


def evidence_ok(row: dict, target: str) -> bool:
    ev = row.get("hk_verified")
    if not isinstance(ev, dict):
        return False
    if not bool(ev.get("segment_ok")):
        return False
    height = int(ev.get("height") or 0)
    if height < floor_for(target):
        return False
    codec = str(ev.get("codec") or "").casefold()
    bitrate = float(ev.get("bitrate_mbps") or 0)
    if codec == "h264" and 0 < bitrate < 2.0:
        return False
    return True


def evidence_score(row: dict, target: str) -> float:
    ev = row.get("hk_verified") or {}
    height = int(ev.get("height") or 0)
    bitrate = float(ev.get("bitrate_mbps") or 0)
    speed = float(ev.get("segment_mbps") or 0)
    startup = float(ev.get("startup_s") or 0)
    score = 100000.0
    score += min(height, 2160) * 10
    score += min(bitrate, 30.0) * 220
    score += min(speed, 100.0) * 15
    score -= min(startup, 30.0) * 30
    return round(score, 3)


def diverse_top(rows: list[dict], target: str, limit: int) -> list[dict]:
    ranked = sorted(rows, key=lambda r: evidence_score(r, target), reverse=True)
    chosen: list[dict] = []
    used_hosts: set[str] = set()
    for row in ranked:
        host = host_of(str(row.get("url") or ""))
        if host and host in used_hosts:
            continue
        chosen.append(row)
        if host:
            used_hosts.add(host)
        if len(chosen) >= limit:
            return chosen
    for row in ranked:
        if row in chosen:
            continue
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen


def fresh_score(row: dict) -> float:
    p = row["probe"]
    if p.get("status") != "GOOD":
        return -1e9
    bitrate = float(p.get("bitrate_mbps") or 0)
    score = 100000.0
    score += min(int(p.get("height") or 0), 2160) * 10
    score += min(bitrate, 30.0) * 220
    score += min(float(p.get("segment_mbps") or 0), 100.0) * 15
    score -= min(float(p.get("startup_s") or 0), 30.0) * 30
    return round(score, 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="harvest/candidates.jsonl")
    ap.add_argument("--playlist", default="tv-core.m3u")
    ap.add_argument("--feedback", default="config/home-route-feedback.json")
    ap.add_argument("--output-dir", default="/var/lib/iptv-hk-probe/candidates")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--fresh-per-channel", type=int, default=4)
    args = ap.parse_args()

    pool = Path(args.pool)
    if not pool.exists():
        raise SystemExit(f"verified pool missing: {pool}")

    formal = hk_probe.load_playlist(Path(args.playlist))
    formal_urls = {name: url for name, url in formal}
    targets = [name for name, _ in formal]
    matcher = legacy.build_target_matcher(targets)
    bad_urls = load_bad_urls(Path(args.feedback))

    matched: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    stats = defaultdict(int)
    for raw in load_rows(pool):
        target = matcher(str(raw.get("name") or ""))
        if not target:
            continue
        url = str(raw.get("url") or "").strip()
        if target == "CCTV-8" and "cctv8k" in url.casefold():
            stats["cctv8k_rejected"] += 1
            continue
        if url in bad_urls:
            stats["home_rejected"] += 1
            continue
        if url == formal_urls.get(target):
            stats["same_as_formal"] += 1
            continue
        if legacy.TOKEN_HINT_RE.search(url):
            stats["token_rejected"] += 1
            continue
        key = (target, url)
        if key in seen:
            continue
        seen.add(key)
        matched[target].append(raw)

    eligible: dict[str, list[dict]] = {}
    selected: list[dict] = []
    for target in targets:
        good = [r for r in matched.get(target, []) if evidence_ok(r, target)]
        eligible[target] = good
        for row in diverse_top(good, target, max(1, args.fresh_per_channel)):
            x = dict(row)
            x["target"] = target
            x["host"] = host_of(str(row.get("url") or ""))
            x["audit_score"] = evidence_score(row, target)
            selected.append(x)

    def fresh_probe(row: dict) -> dict:
        p = asdict(hk_probe.probe_one((str(row["target"]), str(row["url"]))))
        bitrate = float(p.get("bitrate_mbps") or 0)
        if p.get("status") == "GOOD" and str(p.get("codec") or "").casefold() == "h264" and 0 < bitrate < 2.0:
            p["status"] = "DEGRADED"
            p["error"] = f"h264_intrinsic_bitrate_{bitrate:.3f}Mbps_below_2.0"
        out = dict(row)
        out["probe"] = p
        out["score"] = 0.0
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        probed = list(ex.map(fresh_probe, selected))
    for row in probed:
        row["score"] = fresh_score(row)

    by_target: dict[str, list[dict]] = defaultdict(list)
    for row in probed:
        by_target[str(row["target"])].append(row)
    winners: dict[str, dict] = {}
    for target in targets:
        rows = [r for r in by_target.get(target, []) if r["probe"].get("status") == "GOOD"]
        rows.sort(key=lambda r: r["score"], reverse=True)
        if rows:
            winners[target] = rows[0]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe_region": "Hong Kong",
        "stage": "quality_aware_selection_from_fully_verified_pool",
        "production_modified": False,
        "summary": {
            "formal_targets": len(targets),
            "matched_verified_entries": sum(len(v) for v in matched.values()),
            "quality_eligible_entries": sum(len(v) for v in eligible.values()),
            "fresh_probed": len(probed),
            "channels_with_fresh_good_spare": len(winners),
        },
        "policy": {
            "raw_unmeasured_preprobe_truncation": False,
            "full_hk_audit_evidence_required": True,
            "core_height_floor_1080": True,
            "cctv4k_height_floor_2160": True,
            "h264_known_bitrate_floor_mbps": 2.0,
            "home_feedback_hard_veto": True,
            "fresh_probe_after_quality_ranking": True,
        },
        "rejected_before_fresh_probe": dict(stats),
        "eligible_counts": {t: len(eligible.get(t, [])) for t in targets},
        "winners": winners,
        "results": probed,
    }
    (out_dir / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lines = [
        f"verified_matches={payload['summary']['matched_verified_entries']} "
        f"quality_eligible={payload['summary']['quality_eligible_entries']} "
        f"fresh_probed={len(probed)} spares={len(winners)}"
    ]
    for target in targets:
        row = winners.get(target)
        if row:
            p = row["probe"]
            lines.append(
                f"SPARE {target:12} {p.get('width',0)}x{p.get('height',0)} {p.get('codec',''):5} "
                f"bitrate={float(p.get('bitrate_mbps') or 0):.2f}M host={row.get('host','')}"
            )
        else:
            lines.append(f"NONE  {target:12} eligible={len(eligible.get(target, []))}")
    (out_dir / "latest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
