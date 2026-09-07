#!/opt/bin/python3
"""Confirm the living-room path, then activate after a verified GitHub report."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    from .home_contract import ROUTE_CONTEXT, validate_home_report_v2
except ImportError:  # Installed beside this file on the router.
    from home_contract import ROUTE_CONTEXT, validate_home_report_v2  # type: ignore


def parse_utc(value: str) -> float:
    return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def set_actionable(
    config_path: Path,
    *,
    enabled: bool,
    now_epoch: float | None = None,
    report_path: Path | None = None,
) -> dict:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = Path(str(config.get("output_dir") or "/opt/var/lib/iptv-home-probe"))
    if enabled:
        if config.get("route_context") != ROUTE_CONTEXT:
            raise RuntimeError("first confirm the Apple TV-equivalent route in shadow mode")
        if config.get("github_push_enabled") is not True:
            raise RuntimeError("enable the repository-scoped GitHub deploy key before activation")
        if config.get("protected_publishing_ready") is not True:
            raise RuntimeError("protected GitHub publishing is not ready")
        github = json.loads((output / "github-state.json").read_text(encoding="utf-8"))
        report_path = report_path or output / "latest.json"
        raw = report_path.read_bytes()
        report = json.loads(raw.decode("utf-8"))
        validate_home_report_v2(report, expected_probe_id=str(config.get("probe_id") or ""))
        evidence = github.get("successful_report_evidence") or {}
        acknowledged = evidence.get(report.get("generated_utc")) or {}
        digest = hashlib.sha256(raw).hexdigest()
        if not (acknowledged.get("safe") is True and acknowledged.get("probe_id") == config.get("probe_id")):
            raise RuntimeError("need one successfully uploaded household report before activation")
        acknowledged_sha = acknowledged.get("sha256")
        if not acknowledged_sha and github.get("last_report_generated_utc") == report.get("generated_utc"):
            acknowledged_sha = github.get("last_report_sha256")
        if acknowledged_sha != digest:
            raise RuntimeError("GitHub report hash does not match the selected local report")
        generated = parse_utc(str(report.get("generated_utc") or ""))
        if now_epoch - generated > 7 * 3600 or now_epoch - generated < -600:
            raise RuntimeError("latest local report is not fresh")
        if bool((report.get("summary") or {}).get("circuit_breaker_open")):
            raise RuntimeError("latest report opened the mass-failure circuit breaker")
    config["actionable"] = bool(enabled)
    atomic_json(config_path, config)
    return config


def confirm_living_room_path(config_path: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["route_context"] = ROUTE_CONTEXT
    config["actionable"] = False
    atomic_json(config_path, config)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    parser.add_argument("--report", help="An already acknowledged report, when latest.json is a candidate batch")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--off", action="store_true")
    action.add_argument("--confirm-living-room-path", action="store_true")
    args = parser.parse_args()
    try:
        path = Path(args.config)
        if args.confirm_living_room_path:
            confirm_living_room_path(path)
            print("HOME_PROBE_ACTIVATE living-room path confirmed; shadow mode remains on")
        elif args.off:
            set_actionable(path, enabled=False)
            print("HOME_PROBE_ACTIVATE off; GitHub shadow reports continue")
        else:
            set_actionable(path, enabled=True, report_path=Path(args.report) if args.report else None)
            print("HOME_PROBE_ACTIVATE ready; future home decisions may enter protected publishing")
        return 0
    except Exception as exc:
        print(f"HOME_PROBE_ACTIVATE failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
