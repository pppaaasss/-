#!/usr/bin/env python3
"""Validate one AC86U report and publish exact-route replacements atomically."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router.ac86u.home_contract import (  # noqa: E402
    PROBE_ID_RE,
    canonical_name,
    station_key,
    validate_home_report_v2,
)
from router.ac86u.push_home_report import REPORT_LIMIT, report_filename  # noqa: E402
from scripts.home_route_policy import rejected_hosts, rejected_urls  # noqa: E402


CONFIG_SCHEMA = "iptv-home-publisher-config/v1"
RECEIPT_SCHEMA = "iptv-home-publication/v1"
PRODUCTION_FILES = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
REPORT_NAME_RE = re.compile(
    r"^(?P<stamp>[0-9]{8}T[0-9]{6}Z)-(?:primary-0200|recheck-1300)-[0-9a-f]{16}\.json$"
)
UTC = timezone.utc


@dataclass(frozen=True)
class Entry:
    key: str
    name: str
    url: str
    url_index: int


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def utc_text(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("report timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("report timestamp lacks a timezone")
    return parsed.astimezone(UTC)


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: dict) -> None:
    atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def load_config(path: Path) -> dict:
    config = load_object(path)
    if config.get("schema") != CONFIG_SCHEMA:
        raise RuntimeError("unsupported home publisher config")
    if config.get("repository") != "pppaaasss/-" or config.get("report_branch") != "home-reports":
        raise RuntimeError("home publisher destination is not pinned")
    if tuple(config.get("production_files") or ()) != PRODUCTION_FILES:
        raise RuntimeError("home publisher production file set is not exact")
    if config.get("formal_playlist") != "tv-core.m3u":
        raise RuntimeError("home publisher formal playlist is not pinned")
    if config.get("formal_playlist_url") != "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u":
        raise RuntimeError("home publisher formal URL is not pinned")
    if config.get("exact_reported_route_only") is not True:
        raise RuntimeError("home publisher must preserve unreported alternate routes")
    if config.get("branch_protection_required") is not True:
        raise RuntimeError("home publisher requires protected master")
    maximum_age = config.get("maximum_report_age_hours")
    if isinstance(maximum_age, bool) or not isinstance(maximum_age, (int, float)) or not 0 < float(maximum_age) <= 48:
        raise RuntimeError("home publisher report age limit is invalid")
    enabled = config.get("enabled")
    if not isinstance(enabled, bool):
        raise RuntimeError("home publisher enabled flag is invalid")
    probe_id = str(config.get("expected_probe_id") or "")
    if enabled and not PROBE_ID_RE.fullmatch(probe_id):
        raise RuntimeError("enabled home publisher lacks an exact probe ID")
    if config.get("home_feedback") != "config/home-route-feedback.json":
        raise RuntimeError("home publisher feedback path is not pinned")
    if config.get("receipt_path") != "home-publish/latest.json":
        raise RuntimeError("home publisher receipt path is not pinned")
    return config


def playlist_entries(raw: bytes) -> tuple[list[str], list[Entry]]:
    lines = raw.decode("utf-8").splitlines()
    entries: list[Entry] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF") or "," not in line:
            continue
        name = line.rsplit(",", 1)[-1].strip()
        key = station_key(name)
        if key is None:
            continue
        cursor = index + 1
        if cursor >= len(lines) or not lines[cursor].strip().startswith(("http://", "https://")):
            raise RuntimeError(f"{name} has no adjacent HTTP route")
        entries.append(Entry(key=key, name=canonical_name(key), url=lines[cursor].strip(), url_index=cursor))
    return lines, entries


def unique_core_routes(raw: bytes) -> dict[str, Entry]:
    _lines, entries = playlist_entries(raw)
    routes: dict[str, Entry] = {}
    for entry in entries:
        if entry.key in routes:
            raise RuntimeError(f"formal playlist duplicates {entry.key}")
        routes[entry.key] = entry
    if not routes:
        raise RuntimeError("formal playlist has no scoped routes")
    return routes


def latest_report_path(inbox: Path, probe_id: str) -> Path | None:
    directory = inbox / probe_id
    if not directory.exists():
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("home report inbox is not a regular directory")
    candidates: list[tuple[str, Path]] = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"unsafe home report inbox entry: {path.name}")
        match = REPORT_NAME_RE.fullmatch(path.name)
        if not match:
            raise RuntimeError(f"unexpected home report filename: {path.name}")
        candidates.append((str(match.group("stamp")), path))
    if not candidates:
        return None
    latest_stamp = max(stamp for stamp, _path in candidates)
    latest = [path for stamp, path in candidates if stamp == latest_stamp]
    if len(latest) != 1:
        raise RuntimeError("multiple home reports claim the same latest timestamp")
    return latest[0]


def load_report(path: Path, config: dict, *, now_epoch: float) -> tuple[dict, bytes, str]:
    raw = path.read_bytes()
    if not raw or len(raw) > REPORT_LIMIT:
        raise RuntimeError("home report is empty or too large")
    try:
        report = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("home report is not valid UTF-8 JSON") from exc
    validate_home_report_v2(
        report,
        expected_probe_id=str(config["expected_probe_id"]),
        now_epoch=now_epoch,
        max_age_hours=float(config["maximum_report_age_hours"]),
    )
    if path.name != report_filename(report, raw):
        raise RuntimeError("home report filename and content hash disagree")
    return report, raw, sha256_bytes(raw)


def report_binding(report: dict, formal_raw: bytes, config: dict) -> dict[str, Entry]:
    binding = report["formal_playlist"]
    if binding["url"] != config["formal_playlist_url"]:
        raise RuntimeError("home report formal URL is not the pinned production URL")
    if binding["sha256"] != sha256_bytes(formal_raw):
        raise RuntimeError("home report was measured against a different formal playlist")
    formal = unique_core_routes(formal_raw)
    if int(binding["channel_count"]) != len(formal):
        raise RuntimeError("home report formal channel count changed")
    current = {str(row["channel_key"]): row for row in report["current_results"]}
    if set(current) != set(formal):
        raise RuntimeError("home report does not cover the exact formal channel set")
    for key, entry in formal.items():
        if str(current[key]["url"]) != entry.url:
            raise RuntimeError(f"home report current URL changed for {key}")
    return formal


def replacement_plan(report: dict, feedback_path: Path) -> dict[str, dict]:
    candidates = {str(row["candidate_id"]): row for row in report["candidate_results"]}
    current = {str(row["channel_key"]): row for row in report["current_results"]}
    veto_urls = rejected_urls(feedback_path)
    veto_hosts = rejected_hosts(feedback_path, 2)
    replacements: dict[str, dict] = {}
    for decision in report["decisions"]:
        if decision["action"] != "REPLACE":
            continue
        key = str(decision["channel_key"])
        candidate = candidates[str(decision["replacement_candidate_id"])]
        new_url = str(candidate["url"])
        if str(candidate.get("request_options") or ""):
            raise RuntimeError(f"replacement for {key} requires unsupported request options")
        host = (urlsplit(new_url).hostname or "").casefold()
        if new_url in veto_urls or (host and host in veto_hosts):
            raise RuntimeError(f"home feedback vetoes replacement for {key}")
        old_url = str(current[key]["url"])
        if new_url == old_url:
            raise RuntimeError(f"replacement for {key} does not change the route")
        replacements[key] = {
            "channel_key": key,
            "name": canonical_name(key),
            "old_url": old_url,
            "new_url": new_url,
            "candidate_id": str(candidate["candidate_id"]),
        }
    return replacements


def render_exact_replacements(raw: bytes, replacements: dict[str, dict]) -> tuple[bytes, dict[str, int]]:
    lines, entries = playlist_entries(raw)
    counts = {key: 0 for key in replacements}
    for entry in entries:
        plan = replacements.get(entry.key)
        if plan is None or entry.url != plan["old_url"]:
            continue
        lines[entry.url_index] = str(plan["new_url"])
        counts[entry.key] += 1
    ending = "\n" if raw.endswith(b"\n") else ""
    return ("\n".join(lines) + ending).encode("utf-8"), counts


def replay_status(receipt_path: Path, report: dict, report_sha: str) -> str:
    if not receipt_path.exists():
        return "new"
    receipt = load_object(receipt_path)
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise RuntimeError("existing home publication receipt is invalid")
    incoming = parse_utc(str(report["generated_utc"]))
    previous = parse_utc(str(receipt.get("report_generated_utc") or ""))
    if incoming < previous:
        return "older"
    if incoming == previous:
        if receipt.get("report_sha256") == report_sha:
            return "duplicate"
        raise RuntimeError("same-timestamp home report content changed")
    return "new"


def write_transaction(
    root: Path,
    rendered: dict[str, bytes],
    originals: dict[str, bytes],
    receipt_path: Path,
    receipt: dict,
) -> None:
    previous_receipt = receipt_path.read_bytes() if receipt_path.exists() else None
    try:
        for name in PRODUCTION_FILES:
            atomic_bytes(root / name, rendered[name])
        atomic_json(receipt_path, receipt)
    except Exception:
        for name, raw in originals.items():
            atomic_bytes(root / name, raw)
        if previous_receipt is None:
            if receipt_path.exists():
                receipt_path.unlink()
        else:
            atomic_bytes(receipt_path, previous_receipt)
        raise


def publish_latest(
    *,
    root: Path,
    config_path: Path,
    inbox: Path,
    now_epoch: float | None = None,
    apply: bool = False,
) -> dict:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    config = load_config(config_path)
    if config["enabled"] is not True:
        return {"status": "disabled", "replacement_count": 0}
    path = latest_report_path(inbox, str(config["expected_probe_id"]))
    if path is None:
        return {"status": "no_report", "replacement_count": 0}
    report, _report_raw, report_sha = load_report(path, config, now_epoch=now_epoch)
    receipt_path = root / str(config.get("receipt_path") or "home-publish/latest.json")
    replay = replay_status(receipt_path, report, report_sha)
    if replay != "new":
        return {"status": replay, "replacement_count": 0, "report_sha256": report_sha}

    formal_path = root / str(config["formal_playlist"])
    formal_raw = formal_path.read_bytes()
    report_binding(report, formal_raw, config)
    if report.get("actionable") is not True:
        return {"status": "shadow", "replacement_count": 0, "report_sha256": report_sha}
    baseline = report["baseline"]
    if not all(baseline[field] is True for field in ("home_network_ok", "github_reachable", "route_verified")):
        return {"status": "unsafe_baseline", "replacement_count": 0, "report_sha256": report_sha}
    if baseline["mass_failure_circuit_breaker"] is not False:
        return {"status": "circuit_open", "replacement_count": 0, "report_sha256": report_sha}

    feedback_path = root / str(config.get("home_feedback") or "config/home-route-feedback.json")
    replacements = replacement_plan(report, feedback_path)
    originals = {name: (root / name).read_bytes() for name in PRODUCTION_FILES}
    rendered: dict[str, bytes] = {}
    changed_counts: dict[str, dict[str, int]] = {}
    for name in PRODUCTION_FILES:
        rendered[name], changed_counts[name] = render_exact_replacements(originals[name], replacements)
    for key in replacements:
        if changed_counts["tv-core.m3u"].get(key) != 1:
            raise RuntimeError(f"formal playlist replacement count is not exactly one for {key}")

    before = {name: sha256_bytes(raw) for name, raw in originals.items()}
    after = {name: sha256_bytes(raw) for name, raw in rendered.items()}
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "processed_utc": utc_text(now_epoch),
        "probe_id": report["probe_id"],
        "report_file": path.name,
        "report_generated_utc": report["generated_utc"],
        "report_run_kind": report["run_kind"],
        "report_sha256": report_sha,
        "formal_sha256_before": sha256_bytes(formal_raw),
        "formal_sha256_after": after["tv-core.m3u"],
        "production_sha256_before": before,
        "production_sha256_after": after,
        "replacement_count": len(replacements),
        "replacements": [replacements[key] for key in sorted(replacements)],
        "changed_occurrences": changed_counts,
        "policy": {
            "home_is_only_health_authority": True,
            "unknown_never_replaces": True,
            "good_routes_remain_untouched": True,
            "exact_reported_route_only": True,
            "replacement_count_limit": None,
            "all_four_playlists_one_git_snapshot": True,
        },
    }
    if apply:
        write_transaction(root, rendered, originals, receipt_path, receipt)
    return {"status": "applied" if apply else "planned", **receipt}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--config", default="config/home-publisher.json")
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--now-epoch", type=float, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(args.repo_root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        result = publish_latest(
            root=root,
            config_path=config_path,
            inbox=Path(args.inbox),
            now_epoch=args.now_epoch,
            apply=args.apply,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"HOME_PUBLISH rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
