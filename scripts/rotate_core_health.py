#!/usr/bin/env python3
"""Conservatively manage unqualified CCTV/satellite routes every three days.

The Hong Kong host remains the source of network evidence.  This script is the
deterministic production publisher: it reads the latest health report and the
Hong-Kong-verified spare pool, rejects unsafe/ambiguous candidates, and updates
all four formal playlists together.  There is deliberately no per-run channel
limit; every qualifying decision in the current round is applied atomically.

Healthy routes are deliberately sticky.  A higher remote benchmark is not a
reason to disturb a working living-room route.  Home-accepted routes are kept
unless they are later rejected at home or repeatedly confirmed dead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dead_only_failover import PLAYLISTS, TOKEN_RE, canonical, jsonl_rows  # noqa: E402
from scripts.hk_filter_harvest import build_target_matcher  # noqa: E402
from scripts.home_route_policy import load_feedback, rejected_hosts, rejected_urls  # noqa: E402


CCTV8K_RE = re.compile(r"cctv[-_]?8k|cctv8k|/8k(?:[/?.]|$)", re.I)
UTC = timezone.utc


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError("missing UTC timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rotation_due(state: dict, now_utc: datetime, force: bool = False) -> tuple[bool, date]:
    zone = ZoneInfo(str(state.get("timezone") or "Asia/Singapore"))
    today = now_utc.astimezone(zone).date()
    if force:
        return True, today
    last_raw = str(state.get("last_completed_local_date") or "").strip()
    if not last_raw:
        return True, today
    last = date.fromisoformat(last_raw)
    interval = max(1, int(state.get("interval_days") or 3))
    return (today - last).days >= interval, today


def playlist_entries(path: Path) -> list[tuple[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = []
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("#EXTINF") or "," not in line:
            continue
        url = lines[index + 1].strip()
        if not url.startswith(("http://", "https://")):
            continue
        rows.append((canonical(line.rsplit(",", 1)[-1].strip()), url))
    return rows


def playlist_routes(path: Path) -> dict[str, list[str]]:
    routes: dict[str, list[str]] = {}
    for name, url in playlist_entries(path):
        routes.setdefault(name, []).append(url)
    return routes


def render_replacements(path: Path, replacements: dict[str, str]) -> tuple[str, int]:
    before = path.read_text(encoding="utf-8")
    lines = before.splitlines()
    found: set[str] = set()
    changed = 0
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("#EXTINF") or "," not in line:
            continue
        name = canonical(line.rsplit(",", 1)[-1].strip())
        if name not in replacements:
            continue
        if not lines[index + 1].strip().startswith(("http://", "https://")):
            raise RuntimeError(f"{path}: {name} has no adjacent HTTP route")
        found.add(name)
        if lines[index + 1].strip() != replacements[name]:
            lines[index + 1] = replacements[name]
            changed += 1
    missing = set(replacements) - found
    if missing:
        raise RuntimeError(f"{path}: missing replacement targets: {sorted(missing)}")
    rendered = "\n".join(lines) + ("\n" if before.endswith("\n") else "")
    return rendered, changed


def health_rows(report: dict) -> dict[str, dict]:
    rows = {}
    for raw in report.get("results") or []:
        if not isinstance(raw, dict):
            continue
        name = canonical(str(raw.get("name") or ""))
        if name:
            rows[name] = raw
    return rows


def validate_health(report: dict, now_utc: datetime, policy: dict) -> None:
    if bool((report.get("summary") or {}).get("circuit_breaker_open")):
        raise RuntimeError("health circuit breaker is open; refusing production rotation")
    generated = parse_utc(str(report.get("generated_utc") or ""))
    age = now_utc - generated
    maximum = timedelta(hours=float(policy.get("maximum_health_age_hours") or 18))
    if age < timedelta(minutes=-10) or age > maximum:
        raise RuntimeError(f"health report is stale or future-dated: age={age}")


def source_count(rows: list[dict]) -> int:
    return len({str(source) for row in rows for source in (row.get("sources") or []) if str(source)})


def feedback_urls(feedback: dict, kind: str) -> set[str]:
    urls: set[str] = set()
    for entries in (feedback.get(kind) or {}).values():
        for item in entries or []:
            value = item.get("url") if isinstance(item, dict) else item
            url = str(value or "").strip()
            if url:
                urls.add(url)
    return urls


def candidate_pool(
    *,
    path: Path,
    targets: list[str],
    current_routes: dict[str, str],
    bad_urls: set[str],
    blocked_hosts: set[str] | None,
    now_utc: datetime,
    policy: dict,
) -> dict[str, list[dict]]:
    matcher = build_target_matcher(targets)
    grouped: dict[str, list[dict]] = {}
    for row in jsonl_rows(path):
        grouped.setdefault(str(row.get("url") or "").strip(), []).append(row)

    occupied = {url: name for name, url in current_routes.items()}
    output = {target: [] for target in targets}
    minimum_refs = max(1, int(policy.get("minimum_source_references") or 2))
    maximum_age = timedelta(days=float(policy.get("maximum_candidate_age_days") or 7))
    default_floor = int(policy.get("minimum_height_default") or 1080)
    overrides = policy.get("minimum_height_overrides") or {}
    minimum_bitrate = float(policy.get("minimum_h264_bitrate_mbps") or 2.0)

    blocked_hosts = blocked_hosts or set()
    for url, rows in grouped.items():
        if not url or url in bad_urls or TOKEN_RE.search(url):
            continue
        host = (urlsplit(url).hostname or "").casefold()
        if host in blocked_hosts:
            continue
        names = {str(row.get("name") or "").strip() for row in rows}
        matched = {matcher(name) for name in names}
        # Every observed name for one URL must resolve to the same formal tile.
        # This rejects relays that appear under unrelated channel names.
        if None in matched or len(matched) != 1:
            continue
        target = next(iter(matched))
        if not target:
            continue
        if target == "CCTV-8" and CCTV8K_RE.search(url):
            continue
        other = occupied.get(url)
        if other and other != target:
            continue
        if source_count(rows) < minimum_refs:
            continue

        evidence_rows = [row for row in rows if isinstance(row.get("hk_verified"), dict)]
        if not evidence_rows:
            continue
        row = max(
            evidence_rows,
            key=lambda item: str((item.get("hk_verified") or {}).get("checked_utc") or ""),
        )
        evidence = row.get("hk_verified") or {}
        try:
            checked = parse_utc(str(evidence.get("checked_utc") or ""))
        except Exception:
            continue
        age = now_utc - checked
        if age < timedelta(minutes=-10) or age > maximum_age:
            continue
        floor = max(default_floor, int(overrides.get(target) or 0))
        if not evidence.get("segment_ok") or int(evidence.get("height") or 0) < floor:
            continue
        codec = str(evidence.get("codec") or "").casefold()
        bitrate = float(evidence.get("bitrate_mbps") or 0)
        stream_bitrate = float(evidence.get("stream_mbps") or bitrate or 0)
        if codec == "h264" and 0 < stream_bitrate < minimum_bitrate:
            continue
        output[target].append({
            "url": url,
            "host": host,
            "height": int(evidence.get("height") or 0),
            "width": int(evidence.get("width") or 0),
            "fps": float(evidence.get("fps") or 0),
            "bitrate_mbps": bitrate,
            "stream_mbps": stream_bitrate,
            "segment_mbps": float(evidence.get("segment_mbps") or 0),
            "startup_s": float(evidence.get("startup_s") or 0),
            "checked_utc": utc_text(checked),
            "source_references": source_count(rows),
        })
    return output


def candidate_score(row: dict, reused_host: bool = False) -> float:
    score = min(int(row.get("height") or 0), 2160) * 10.0
    if float(row.get("fps") or 0) >= 45:
        score += 5.0
    stream = float(row.get("stream_mbps") or row.get("bitrate_mbps") or 0)
    if stream > 0:
        score += min(stream, 20.0) * 40.0
    else:
        score -= 20.0
    score += min(float(row.get("segment_mbps") or 0), 12.0) * 2.0
    score -= min(float(row.get("startup_s") or 0), 10.0) * 10.0
    score += min(int(row.get("source_references") or 0), 8) * 2.0
    if reused_host:
        score -= 8.0
    return round(score, 3)


def clearly_better(current: dict, candidate: dict, policy: dict) -> bool:
    current_height = int(current.get("height") or 0)
    current_fps = float(current.get("fps") or 0)
    if current_height and int(candidate.get("height") or 0) < current_height:
        return False
    if current_fps >= 45 and float(candidate.get("fps") or 0) < 45:
        return False
    old_speed = float(current.get("segment_mbps") or 0)
    new_speed = float(candidate.get("segment_mbps") or 0)
    old_start = float(current.get("startup_s") or 0)
    new_start = float(candidate.get("startup_s") or 0)
    minimum_speed = float(policy.get("minimum_candidate_segment_mbps") or 2.0)
    if old_speed <= 0:
        return new_speed >= minimum_speed
    speed_gate = max(minimum_speed, old_speed * 1.30, old_speed + 0.50)
    startup_gain = old_start - new_start
    return new_speed >= speed_gate and (startup_gain >= 0.50 or new_speed >= old_speed * 1.80)


def weak_current(row: dict, policy: dict) -> bool:
    if str(row.get("status") or "") != "GOOD":
        return False
    return (
        float(row.get("segment_mbps") or 0) < float(policy.get("weak_segment_mbps_below") or 2.5)
        and float(row.get("startup_s") or 0) > float(policy.get("weak_startup_seconds_above") or 2.0)
    )


def dead_approvals(report: dict | None) -> dict[tuple[str, str], str]:
    approvals = {}
    if not report:
        return approvals
    for row in report.get("selected_updates") or []:
        if not isinstance(row, dict):
            continue
        name = canonical(str(row.get("channel") or ""))
        old_url = str(row.get("old_url") or "").strip()
        new_url = str(row.get("new_url") or "").strip()
        if name and old_url and new_url:
            approvals[(name, old_url)] = new_url
    return approvals


def choose_candidate(
    candidates: list[dict],
    *,
    current_url: str,
    current_health: dict,
    reason: str,
    policy: dict,
    selected_hosts: set[str],
    approved_dead_url: str | None = None,
) -> dict | None:
    eligible = []
    for row in candidates:
        if row["url"] == current_url:
            continue
        if approved_dead_url is not None and row["url"] != approved_dead_url:
            continue
        if reason == "clearly_weak" and not clearly_better(current_health, row, policy):
            continue
        if (
            reason == "quality_degraded"
            and policy.get("require_known_stream_bitrate_for_quality_upgrade", True)
            and float(row.get("stream_mbps") or row.get("bitrate_mbps") or 0) <= 0
        ):
            continue
        eligible.append(row)
    if not eligible:
        return None
    return max(eligible, key=lambda row: candidate_score(row, row.get("host") in selected_hosts))


def run_rotation(
    *,
    root: Path,
    state_path: Path,
    health_path: Path,
    failover_path: Path | None,
    report_path: Path,
    now_utc: datetime,
    force: bool = False,
    apply: bool = False,
) -> dict:
    state = load_json(state_path)
    if not state.get("enabled", True):
        return {"due": False, "reason": "disabled", "replacement_count": 0}
    due, local_day = rotation_due(state, now_utc, force)
    if not due:
        print(f"CORE_HEALTH_ROTATION not_due local_date={local_day.isoformat()}")
        return {"due": False, "reason": "not_due", "replacement_count": 0}

    policy = state.get("policy") or {}
    if policy.get("no_replacement_count_limit") is not True:
        raise RuntimeError("rotation policy must explicitly declare no replacement count limit")
    health = load_json(health_path)
    validate_health(health, now_utc, policy)
    failover = load_json(failover_path) if failover_path and failover_path.exists() else None
    approvals = dead_approvals(failover)
    feedback_path = root / str(state.get("home_feedback") or "config/home-route-feedback.json")
    feedback = load_feedback(feedback_path)
    bad = rejected_urls(feedback_path)
    accepted = feedback_urls(feedback, "good")
    blocked_hosts = rejected_hosts(
        feedback_path,
        int(policy.get("candidate_host_block_after_home_failures") or 0),
    )

    formal = {name: playlist_routes(root / name) for name in PLAYLISTS}
    core_order = []
    current_routes = {}
    for name, url in playlist_entries(root / "tv-core.m3u"):
        if name in current_routes and current_routes[name] != url:
            raise RuntimeError(f"tv-core.m3u has conflicting routes for {name}")
        if name not in current_routes:
            core_order.append(name)
        current_routes[name] = url

    pool = candidate_pool(
        path=root / str(state.get("verified_pool") or "harvest/candidates.jsonl"),
        targets=core_order,
        current_routes=current_routes,
        bad_urls=bad,
        blocked_hosts=blocked_hosts,
        now_utc=now_utc,
        policy=policy,
    )
    health_by_name = health_rows(health)
    replacements = {}
    selected_hosts: set[str] = set()
    decisions = []

    for name in core_order:
        current_url = current_routes[name]
        all_urls = {url for routes in formal.values() for url in routes.get(name, [])}
        current_health = health_by_name.get(name)
        reason = ""
        approved_dead_url = None

        if all_urls & bad:
            reason = "home_feedback_rejected"
        elif not current_health:
            decisions.append({"channel": name, "action": "kept", "reason": "missing_health_row"})
            continue
        elif str(current_health.get("url") or "").strip() != current_url:
            decisions.append({"channel": name, "action": "kept", "reason": "health_route_not_current"})
            continue
        elif str(current_health.get("status") or "") == "DEAD":
            approved_dead_url = approvals.get((name, current_url))
            if not approved_dead_url:
                decisions.append({
                    "channel": name,
                    "action": "kept",
                    "reason": "dead_waiting_secondary_confirmation",
                })
                continue
            reason = "confirmed_dead"
        elif (
            policy.get("home_accepted_routes_are_locked", True)
            and current_url in accepted
        ):
            decisions.append({"channel": name, "action": "kept", "reason": "home_accepted_lock"})
            continue
        elif str(current_health.get("status") or "") == "DEGRADED":
            reason = "quality_degraded"
        elif policy.get("performance_rotation_enabled", False) and weak_current(current_health, policy):
            reason = "clearly_weak"
        else:
            decisions.append({"channel": name, "action": "kept", "reason": "healthy"})
            continue

        chosen = choose_candidate(
            pool.get(name, []),
            current_url=current_url,
            current_health=current_health or {},
            reason=reason,
            policy=policy,
            selected_hosts=selected_hosts,
            approved_dead_url=approved_dead_url,
        )
        if chosen is None:
            decisions.append({
                "channel": name,
                "action": "kept",
                "reason": f"{reason}_without_qualified_backup",
            })
            continue
        replacements[name] = chosen["url"]
        if chosen.get("host"):
            selected_hosts.add(str(chosen["host"]))
        decisions.append({
            "channel": name,
            "action": "replaced",
            "reason": reason,
            "old_url": current_url,
            "new_url": chosen["url"],
            "evidence": {key: chosen[key] for key in (
                "width", "height", "fps", "bitrate_mbps", "stream_mbps", "segment_mbps",
                "startup_s", "checked_utc", "source_references",
            )},
        })

    rendered = {}
    changed_files = []
    changed_routes = {}
    for filename in PLAYLISTS:
        text, changed = render_replacements(root / filename, replacements)
        rendered[filename] = text
        changed_routes[filename] = changed
        if changed:
            changed_files.append(filename)
    unresolved = [
        row["channel"] for row in decisions
        if row["action"] == "kept" and "without_qualified_backup" in row["reason"]
    ]
    result = {
        "generated_utc": utc_text(now_utc),
        "local_date": local_day.isoformat(),
        "due": True,
        "applied": bool(apply),
        "production_modified": bool(replacements),
        "replacement_count": len(replacements),
        "replacement_limit": None,
        "synchronized_playlists": list(PLAYLISTS),
        "changed_files": changed_files,
        "changed_routes_per_file": changed_routes,
        "unresolved_channels": unresolved,
        "decisions": decisions,
        "policy": {
            "interval_days": int(state.get("interval_days") or 3),
            "no_replacement_count_limit": True,
            "all_four_playlists_atomic": True,
            "home_feedback_veto": True,
            "hong_kong_verified_candidates_only": True,
            "cctv4k_height_floor": 2160,
            "dead_requires_secondary_confirmation": True,
            "performance_rotation_enabled": bool(policy.get("performance_rotation_enabled", False)),
            "home_accepted_routes_are_locked": bool(policy.get("home_accepted_routes_are_locked", True)),
            "blocked_candidate_hosts": sorted(blocked_hosts),
        },
    }

    if apply:
        for filename, text in rendered.items():
            (root / filename).write_text(text, encoding="utf-8", newline="\n")
        state["last_completed_local_date"] = local_day.isoformat()
        state["last_completed_utc"] = utc_text(now_utc)
        state["last_result"] = {
            "replacement_count": len(replacements),
            "unresolved_channels": unresolved,
        }
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        f"CORE_HEALTH_ROTATION due=1 replacements={len(replacements)} "
        f"unresolved={len(unresolved)} unlimited=1 applied={int(bool(apply))}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--state", default="config/core-health-rotation.json")
    parser.add_argument("--health-report", required=True)
    parser.add_argument("--failover-report", default="")
    parser.add_argument("--report", default="rotation/latest.json")
    parser.add_argument("--now-utc", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(args.repo_root).resolve()
    now = parse_utc(args.now_utc) if args.now_utc else datetime.now(UTC)
    try:
        run_rotation(
            root=root,
            state_path=root / args.state,
            health_path=Path(args.health_report),
            failover_path=Path(args.failover_report) if args.failover_report else None,
            report_path=root / args.report,
            now_utc=now,
            force=args.force,
            apply=args.apply,
        )
        return 0
    except Exception as exc:
        print(f"CORE_HEALTH_ROTATION failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
