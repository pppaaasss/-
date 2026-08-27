#!/usr/bin/env python3
"""Conservatively promote Hong Kong-verified spare routes into formal playlists.

This program never replaces healthy/unknown formal routes. A formal route must
be repeatedly DEGRADED or repeatedly hard-dead, while the same spare URL must
be repeatedly GOOD. At most a small number of channels are changed per run.
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


def hard_dead(error: str, patterns: list[str]) -> bool:
    low = (error or "").casefold()
    return any(str(p).casefold() in low for p in patterns)


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
    ap.add_argument("--state", required=True)
    ap.add_argument("--config", default="config/hk-auto-update.json")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--summary", default="/var/lib/iptv-hk-probe/auto-update-summary.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_json(Path(args.config), {})
    if not cfg.get("enabled"):
        print("HK_AUTO_UPDATE disabled")
        return 0

    formal_report = load_json(Path(args.formal_report), {})
    candidate_report = load_json(Path(args.candidate_report), {})
    formal = report_map(formal_report)
    winners = candidate_report.get("winners") or {}
    state_path = Path(args.state)
    state = load_json(state_path, {"version": 1, "channels": {}})
    channels = state.setdefault("channels", {})
    protected = {canonical(x) for x in cfg.get("protected_channels") or []}
    patterns = list(cfg.get("hard_dead_error_patterns") or [])

    eligible = []
    decisions = []
    all_names = sorted(set(formal) | {canonical(k) for k in winners})
    for name in all_names:
        f = formal.get(name) or {}
        w = winners.get(name) or winners.get(name.replace("CCTV-", "cctv")) or {}
        current_url = str(f.get("url") or "")
        candidate_url = str(w.get("url") or "")
        st = channels.get(name) if isinstance(channels.get(name), dict) else {}

        if st.get("formal_url") != current_url:
            st["formal_bad_kind"] = ""
            st["formal_bad_runs"] = 0
        st["formal_url"] = current_url

        status = str(f.get("status") or "UNKNOWN")
        err = str(f.get("error") or "")
        bad_kind = "degraded" if status == "DEGRADED" else ("hard_dead" if status == "UNKNOWN" and hard_dead(err, patterns) else "")
        if bad_kind:
            st["formal_bad_runs"] = int(st.get("formal_bad_runs") or 0) + 1 if st.get("formal_bad_kind") == bad_kind else 1
            st["formal_bad_kind"] = bad_kind
        else:
            st["formal_bad_kind"] = ""
            st["formal_bad_runs"] = 0

        ok, reason = candidate_ok(name, w, cfg)
        if st.get("candidate_url") != candidate_url:
            st["candidate_good_runs"] = 0
        st["candidate_url"] = candidate_url
        st["candidate_good_runs"] = int(st.get("candidate_good_runs") or 0) + 1 if ok else 0
        channels[name] = st

        need_bad = int(cfg.get("required_formal_degraded_runs", 2)) if bad_kind == "degraded" else int(cfg.get("required_formal_hard_dead_runs", 3))
        need_good = int(cfg.get("required_candidate_good_runs", 2))
        can = (
            name not in protected
            and bad_kind in {"degraded", "hard_dead"}
            and int(st.get("formal_bad_runs") or 0) >= need_bad
            and ok
            and int(st.get("candidate_good_runs") or 0) >= need_good
            and candidate_url
            and candidate_url != current_url
        )
        decisions.append({
            "channel": name,
            "formal_status": status,
            "formal_bad_kind": bad_kind,
            "formal_bad_runs": st.get("formal_bad_runs", 0),
            "candidate_good_runs": st.get("candidate_good_runs", 0),
            "candidate_reason": reason,
            "protected": name in protected,
            "eligible": can,
            "old_url": current_url,
            "new_url": candidate_url,
        })
        if can:
            eligible.append((name, candidate_url))

    max_updates = max(0, int(cfg.get("max_updates_per_run", 2)))
    selected = eligible[:max_updates]
    repo = Path(args.repo_root)
    if not args.dry_run:
        for name, url in selected:
            for fn in PLAYLISTS:
                replace_one(repo / fn, name, url)

    state["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = {
        "generated_utc": state["updated_utc"],
        "dry_run": args.dry_run,
        "selected_updates": [{"channel": n, "url": u} for n, u in selected],
        "eligible_count": len(eligible),
        "max_updates_per_run": max_updates,
        "protected_channels": sorted(protected),
        "decisions": decisions,
    }
    sp = Path(args.summary)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"HK_AUTO_UPDATE eligible={len(eligible)} selected={len(selected)} dry_run={args.dry_run}")
    for name, url in selected:
        print(f"UPDATE {name} -> {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
