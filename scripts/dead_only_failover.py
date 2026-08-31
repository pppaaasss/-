#!/usr/bin/env python3
"""Replace only repeatedly confirmed DEAD routes with freshly probed spares.

The normal path keeps using the fixed, previously verified core spare. Only
when a formal route is confirmed DEAD does this module look at additional
Hong-Kong-verified routes and newly harvested GitHub candidates. Newly
harvested routes are never trusted on metadata alone: every selected route is
probed repeatedly from Hong Kong before a production playlist can change.
"""

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
from scripts.hk_filter_harvest import build_target_matcher, diverse_take  # noqa: E402
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
    return {
        str(row.get("url") or "").strip(): row
        for row in jsonl_rows(path)
        if str(row.get("url") or "").strip()
    }


def jsonl_rows(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if isinstance(row, dict) and str(row.get("url") or "").strip():
            rows.append(row)
    return rows


def matching_rows(path: Path, channel: str) -> list[dict]:
    """Return de-duplicated harvested rows matching one formal channel."""
    matcher = build_target_matcher([channel])
    by_url: dict[str, dict] = {}
    for row in jsonl_rows(path):
        if matcher(str(row.get("name") or "")) != channel:
            continue
        url = str(row.get("url") or "").strip()
        previous = by_url.get(url)
        if previous is None or len(row.get("sources") or []) > len(previous.get("sources") or []):
            by_url[url] = row
    return list(by_url.values())


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
    if int(result.height or 0) < int(result.min_height or 0):
        return False
    bitrate = float(result.bitrate_mbps or 0)
    if result.codec.casefold() == "h264" and 0 < bitrate < float(cfg.get("minimum_h264_bitrate_mbps", 2.0)):
        return False
    return True


def route_allowed(name: str, url: str, row: dict, cfg: dict, bad: set[str], old_url: str) -> bool:
    if not url or url == old_url or url in bad:
        return False
    if cfg.get("reject_token_urls", True) and TOKEN_RE.search(url):
        return False
    if len(row.get("sources") or []) < int(cfg.get("minimum_source_references", 1)):
        return False
    if name == "CCTV-8" and re.search(r"cctv[-_]?8k|cctv8k|/8k(?:[/?.]|$)", url, re.I):
        return False
    return True


def verified_quality_key(row: dict) -> tuple:
    evidence = row.get("hk_verified") or {}
    return (
        int(evidence.get("height") or 0),
        float(evidence.get("bitrate_mbps") or 0),
        float(evidence.get("segment_mbps") or 0),
        len(row.get("sources") or []),
        -float(evidence.get("startup_s") or 999),
    )


def candidate_queue(
    *,
    root: Path,
    cfg: dict,
    name: str,
    old_url: str,
    floor: int,
    fixed_url: str,
    evidence: dict[str, dict],
    bad: set[str],
) -> tuple[list[dict], list[str]]:
    """Build a bounded, stable-first queue for one confirmed-dead channel."""
    queue: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()

    def add(url: str, row: dict, kind: str, *, require_old_evidence: bool) -> None:
        if url in seen or not route_allowed(name, url, row, cfg, bad, old_url):
            return
        if require_old_evidence:
            ok, reason = evidence_ok(name, url, row, cfg, floor)
            if not ok:
                skipped.append(f"{kind}:{reason}")
                return
        seen.add(url)
        queue.append({"url": url, "kind": kind, "sources": list(row.get("sources") or [])})

    fixed_row = evidence.get(fixed_url) or {}
    if fixed_url:
        add(fixed_url, fixed_row, "fixed", require_old_evidence=True)

    verified_limit = max(0, int(cfg.get("maximum_verified_candidates_per_channel", 3)))
    verified_path = root / str(cfg["verified_pool"])
    verified = sorted(matching_rows(verified_path, name), key=verified_quality_key, reverse=True)
    verified = diverse_take(verified, verified_limit) if verified_limit else []
    for row in verified:
        url = str(row.get("url") or "").strip()
        add(url, row, "verified_pool", require_old_evidence=True)

    if cfg.get("dynamic_pending_enabled", True):
        pending_limit = max(0, int(cfg.get("maximum_pending_candidates_per_channel", 8)))
        pending_path = root / str(cfg.get("pending_candidate_pool", "harvest/pending.jsonl"))
        pending = matching_rows(pending_path, name)
        pending.sort(key=lambda row: (len(row.get("sources") or []), str(row.get("url") or "")), reverse=True)
        pending = diverse_take(pending, pending_limit) if pending_limit else []
        for row in pending:
            url = str(row.get("url") or "").strip()
            # Pending means unverified. Live Hong Kong probes below are the only
            # evidence allowed to promote one into a formal playlist.
            add(url, row, "github_pending", require_old_evidence=False)

    return queue, skipped


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
    configured_maximum = int(cfg.get("maximum_updates_per_cycle", 0))
    maximum = configured_maximum if configured_maximum > 0 else None

    for row in report.get("results") or []:
        if maximum is not None and len(selected) >= maximum:
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
        floor = max(
            int(row.get("min_height") or cfg.get("minimum_height_default", 1080)),
            int((cfg.get("minimum_height_overrides") or {}).get(name, 0)),
        )

        current_attempts = []
        for _ in range(max(1, int(cfg.get("current_recheck_attempts", 2)))):
            check = probe((name, old_url, floor))
            current_attempts.append(asdict(check))
            if check.status in {"GOOD", "DEGRADED"}:
                break
        if any(item["status"] in {"GOOD", "DEGRADED"} for item in current_attempts):
            decisions.append({"channel": name, "reason": "current_route_recovered"})
            continue

        queue, skipped = candidate_queue(
            root=root,
            cfg=cfg,
            name=name,
            old_url=old_url,
            floor=floor,
            fixed_url=str(candidates.get(name) or "").strip(),
            evidence=evidence,
            bad=bad,
        )
        required_attempts = max(1, int(cfg.get("candidate_confirm_attempts", 2)))
        chosen = None
        failed = []
        for candidate in queue:
            attempts = []
            for _ in range(required_attempts):
                check = probe((name, candidate["url"], floor))
                attempts.append(asdict(check))
                if not fresh_candidate_ok(check, cfg):
                    break
            if len(attempts) == required_attempts and all(
                fresh_candidate_ok(hk_probe.ProbeResult(**item), cfg) for item in attempts
            ):
                chosen = candidate
                break
            failed.append({"kind": candidate["kind"], "url": candidate["url"]})

        if chosen is None:
            decisions.append({
                "channel": name,
                "reason": "no_qualified_spare_after_fresh_probe",
                "candidate_count": len(queue),
                "failed_candidates": failed,
                "skipped": skipped,
            })
            continue
        new_url = chosen["url"]
        selected.append({
            "channel": name,
            "old_url": old_url,
            "new_url": new_url,
            "source_kind": chosen["kind"],
            "matching_files": matching_files,
            "floor": floor,
        })
        decisions.append({
            "channel": name,
            "reason": "confirmed_dead_qualified_spare_ready",
            "source_kind": chosen["kind"],
            "candidate_count": len(queue),
        })

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
            "no_replacement_count_limit": maximum is None,
            "fixed_candidate_playlist": str(cfg["fixed_candidate_playlist"]),
            "github_pending_on_demand": bool(cfg.get("dynamic_pending_enabled", True)),
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
