#!/usr/bin/env python3
"""Promote Hong Kong-verified spare routes into formal playlists.

There are no artificial per-run or protected-channel limits. If the current
formal route is DEGRADED/UNKNOWN in Hong Kong and a replacement passes the
hard identity/quality checks, replace that channel across all formal lists.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

PLAYLISTS = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
TOKEN_RE = re.compile(r"(?:^|[?&])(token|auth_key|expires?|expire|sign|wssecret|wstime)=", re.I)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def canonical(name: str) -> str:
    raw = name.strip()
    low = raw.casefold().replace("＋", "+")
    if low in {"cctv5+", "cctv-5+", "cctv5plus", "cctv-5plus"}:
        return "CCTV-5+"
    if low in {"cctv4k", "cctv-4k"}:
        return "CCTV-4K"
    m = re.fullmatch(r"cctv-?(\d{1,2})", low)
    if m:
        return f"CCTV-{int(m.group(1))}"
    return raw


def report_map(report: dict) -> dict[str, dict]:
    out = {}
    for row in report.get("results") or []:
        if isinstance(row, dict) and row.get("name"):
            out[canonical(str(row["name"]))] = row
    return out


def candidate_ok(channel: str, row: dict, cfg: dict) -> tuple[bool, str]:
    if not isinstance(row, dict):
        return False, "no_candidate"
    url = str(row.get("url") or "").strip()
    probe = row.get("probe") or {}
    if not url or probe.get("status") != "GOOD":
        return False, "candidate_not_good"
    if channel == "CCTV-8" and "cctv8k" in url.casefold():
        return False, "cctv8k_identity_rejected"
    if cfg.get("reject_token_urls", True) and (row.get("token_hint") or TOKEN_RE.search(url)):
        return False, "token_url_rejected"
    floor = int((cfg.get("minimum_height_overrides") or {}).get(channel, cfg.get("minimum_height_default", 1080)))
    if int(probe.get("height") or 0) < floor:
        return False, "candidate_height_below_floor"
    codec = str(probe.get("codec") or "").casefold()
    bitrate = float(probe.get("bitrate_mbps") or 0)
    if codec == "h264" and bitrate > 0 and bitrate < float(cfg.get("minimum_h264_bitrate_mbps", 2.0)):
        return False, "candidate_h264_bitrate_too_low"
    if codec == "h264" and bitrate <= 0 and not cfg.get("allow_unknown_h264_bitrate", True):
        return False, "candidate_h264_bitrate_unknown"
    if not probe.get("segment_ok"):
        return False, "candidate_segment_not_verified"
    return True, "ok"


def replace_one(path: Path, channel: str, new_url: str) -> None:
    before = path.read_text(encoding="utf-8")
    lines = before.splitlines()
    hits = []
    for i, line in enumerate(lines[:-1]):
        if not line.startswith("#EXTINF:") or "," not in line:
            continue
        if canonical(line.rsplit(",", 1)[-1]) == channel:
            hits.append(i)
    if len(hits) != 1:
        raise RuntimeError(f"{path}: expected exactly one {channel}, got {len(hits)}")
    i = hits[0]
    if lines[i + 1].strip() == new_url:
        return
    lines[i + 1] = new_url
    path.write_text("\n".join(lines) + ("\n" if before.endswith("\n") else ""), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formal-report", required=True)
    ap.add_argument("--candidate-report", required=True)
    ap.add_argument("--config", default="config/hk-auto-update.json")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--summary", default="/var/lib/iptv-hk-probe/auto-update-summary.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_json(Path(args.config), {})
    if not cfg.get("enabled"):
        print("HK_AUTO_UPDATE disabled")
        return 0

    formal = report_map(load_json(Path(args.formal_report), {}))
    candidate_report = load_json(Path(args.candidate_report), {})
    winners = candidate_report.get("winners") or {}
    replace_statuses = {str(x).upper() for x in cfg.get("replace_formal_statuses") or ["DEGRADED", "UNKNOWN"]}

    selected = []
    decisions = []
    for name, f in sorted(formal.items()):
        w = winners.get(name) or {}
        current_url = str(f.get("url") or "").strip()
        candidate_url = str(w.get("url") or "").strip()
        status = str(f.get("status") or "UNKNOWN").upper()
        ok, reason = candidate_ok(name, w, cfg)
        can = status in replace_statuses and ok and candidate_url and candidate_url != current_url
        decisions.append({
            "channel": name,
            "formal_status": status,
            "candidate_reason": reason,
            "eligible": can,
            "old_url": current_url,
            "new_url": candidate_url,
        })
        if can:
            selected.append((name, candidate_url))

    repo = Path(args.repo_root)
    if not args.dry_run:
        for name, url in selected:
            for fn in PLAYLISTS:
                replace_one(repo / fn, name, url)

    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary = {
        "generated_utc": generated,
        "dry_run": args.dry_run,
        "selected_updates": [{"channel": n, "url": u} for n, u in selected],
        "eligible_count": len(selected),
        "artificial_update_limit": False,
        "protected_channels": [],
        "decisions": decisions,
    }
    sp = Path(args.summary)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"HK_AUTO_UPDATE selected={len(selected)} dry_run={args.dry_run}")
    for name, url in selected:
        print(f"UPDATE {name} -> {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
