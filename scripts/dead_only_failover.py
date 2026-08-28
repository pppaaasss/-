#!/usr/bin/env python3
"""Replace only repeatedly confirmed DEAD routes with fixed, re-probed spares."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import hk_probe  # noqa: E402
from scripts.home_route_policy import rejected_urls  # noqa: E402


PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
TOKEN_RE = re.compile(r"(?:^|[?&])(token|auth_key|expires?|expire|sign|wssecret|wstime)=", re.I)


def canonical(name: str) -> str:
    raw = str(name or "").strip()
    low = raw.casefold().replace("＋", "+")
    if low in {"cctv5+", "cctv-5+", "cctv5plus", "cctv-5plus"}:
        return "CCTV-5+"
    if low in {"cctv4k", "cctv-4k"}:
        return "CCTV-4K"
    match = re.fullmatch(r"cctv-?(\d{1,2})", low)
    return f"CCTV-{int(match.group(1))}" if match else raw


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def playlist_routes(path: Path) -> dict[str, str]:
    rows = {}
    for name, url in hk_probe.load_playlist(path):
        key = canonical(name)
        if key in rows and rows[key] != url:
            raise RuntimeError(f"{path}: duplicate channel {key} has conflicting routes")
        rows[key] = url
    return rows


def verified_rows(path: Path) -> dict[str, dict]:
    rows = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        url = str(row.get("url") or "").strip() if isinstance(row, dict) else ""
        if url:
            rows[url] = row
    return rows


def evidence_ok(name: str, url: str, row: dict, cfg: dict, floor: int) -> tuple[bool, str]:
    if not row:
        return False, "missing_fixed_audit_evidence"
    evidence = row.get("hk_verified") or {}
    if not evidence.get("segment_ok"):
        return False, "fixed_evidence_segment_failed"
    if int(evidence.get("height") or 0) < floor:
        return False, "fixed_evidence_below_floor"
    if len(row.get("sources") or []) < int(cfg.get("minimum_source_references", 1)):
        return False, "insufficient_source_references"
    if cfg.get("reject_token_urls", True) and TOKEN_RE.search(url):
        return False, "token_url_rejected"
    if name == "CCTV-8" and re.search(r"cctv[-_]?8k|cctv8k|/8k(?:[/?.]|$)", url, re.I):
        return False, "cctv8k_identity_rejected"
    return True, "ok"


def fresh_candidate_ok(result: hk_probe.ProbeResult, cfg: dict) -> bool:
    if result.status != "GOOD" or not result.segment_ok:
        return False
    bitrate = float(result.bitrate_mbps or 0)
    if result.codec.casefold() == "h264" and 0 < bitrate < float(cfg.get("minimum_h264_bitrate_mbps", 2.0)):
        return False
    return True


def replace_exact(path: Path, channel: str, old_url: str, new_url: str) -> bool:
    before = path.read_text(encoding="utf-8")
    lines = before.splitlines()
    hits = []
    for index, line in enumerate(lines[:-1]):
        if line.startswith("#EXTINF:") and "," in line and canonical(line.rsplit(",", 1)[-1]) == channel:
            hits.append(index)
    matching = [index for index in hits if lines[index + 1].strip() == old_url]
    if not matching:
        return False
    for index in matching:
        lines[index + 1] = new_url
    path.write_text("\n".join(lines) + ("\n" if before.endswith("\n") else ""), encoding="utf-8")
    return True


def run(args, probe=hk_probe.probe_one) -> dict:
    root = Path(args.repo_root)
    cfg = load_json(root / args.config)
    report = load_json(Path(args.formal_report))
    summary = report.get("summary") or {}
    decisions = []
    selected = []
    if not cfg.get("enabled"):
        return {"applied": False, "selected_updates": [], "decisions": [{"reason": "disabled"}]}
    if summary.get("circuit_breaker_open"):
        return {"applied": False, "selected_updates": [], "decisions": [{"reason": "circuit_breaker_open"}]}

    candidates = playlist_routes(root / str(cfg["fixed_candidate_playlist"]))
    evidence = verified_rows(root / str(cfg["verified_pool"]))
    bad = rejected_urls(root / str(cfg["home_feedback"]))
    formal = {name: playlist_routes(root / name) for name in PLAYLISTS}
    maximum = max(1, int(cfg.get("maximum_updates_per_cycle", 3)))

    for row in report.get("results") or []:
        if len(selected) >= maximum:
            break
        name = canonical(str(row.get("name") or ""))
        old_url = str(row.get("url") or "").strip()
        if row.get("status") != "DEAD" or not row.get("hk_dead_confirmed"):
            continue
        if int(row.get("consecutive_failures") or 0) < 3 or float(row.get("failure_age_hours") or 0) < 6:
            decisions.append({"channel": name, "reason": "dead_threshold_not_proven"})
            continue
        matching_files = [file for file, routes in formal.items() if routes.get(name) == old_url]
        if not matching_files or "tv.m3u" not in matching_files:
            decisions.append({"channel": name, "reason": "stale_or_non_main_route"})
            continue
        new_url = str(candidates.get(name) or "").strip()
        if not new_url or new_url == old_url:
            decisions.append({"channel": name, "reason": "no_fixed_spare"})
            continue
        if new_url in bad:
            decisions.append({"channel": name, "reason": "home_feedback_rejected"})
            continue
        floor = max(
            int(row.get("min_height") or cfg.get("minimum_height_default", 1080)),
            int((cfg.get("minimum_height_overrides") or {}).get(name, 0)),
        )
        ok, reason = evidence_ok(name, new_url, evidence.get(new_url) or {}, cfg, floor)
        if not ok:
            decisions.append({"channel": name, "reason": reason})
            continue

        current_attempts = []
        for _ in range(max(1, int(cfg.get("current_recheck_attempts", 2)))):
            check = probe((name, old_url, floor))
            current_attempts.append(asdict(check))
            if check.status in {"GOOD", "DEGRADED"}:
                break
        if any(item["status"] in {"GOOD", "DEGRADED"} for item in current_attempts):
            decisions.append({"channel": name, "reason": "current_route_recovered"})
            continue

        candidate_attempts = []
        for _ in range(max(1, int(cfg.get("candidate_confirm_attempts", 2)))):
            check = probe((name, new_url, floor))
            candidate_attempts.append(asdict(check))
            if not fresh_candidate_ok(check, cfg):
                break
        if len(candidate_attempts) < max(1, int(cfg.get("candidate_confirm_attempts", 2))) or not all(
            fresh_candidate_ok(hk_probe.ProbeResult(**item), cfg) for item in candidate_attempts
        ):
            decisions.append({"channel": name, "reason": "fixed_spare_fresh_probe_failed"})
            continue
        selected.append({
            "channel": name,
            "old_url": old_url,
            "new_url": new_url,
            "matching_files": matching_files,
            "floor": floor,
        })
        decisions.append({"channel": name, "reason": "confirmed_dead_fixed_spare_ready"})

    changed_files = set()
    if args.apply:
        for update in selected:
            for file in update["matching_files"]:
                if replace_exact(root / file, update["channel"], update["old_url"], update["new_url"]):
                    changed_files.add(file)
    return {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "applied": bool(args.apply),
        "selected_updates": selected,
        "changed_files": sorted(changed_files),
        "decisions": decisions,
        "policy": {
            "dead_only": True,
            "fixed_candidate_playlist": str(cfg["fixed_candidate_playlist"]),
            "home_feedback_veto": True,
            "current_route_rechecked": True,
            "candidate_rechecked_twice": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--formal-report", required=True)
    parser.add_argument("--config", default="config/dead-only-failover.json")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = run(args)
    destination = Path(args.summary)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"DEAD_ONLY_FAILOVER selected={len(result.get('selected_updates') or [])} applied={result.get('applied')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
