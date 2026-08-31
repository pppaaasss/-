#!/opt/bin/python3
"""Activate home evidence only after a complete 24-hour shadow window."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
from pathlib import Path


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
    confirm_living_room_path: bool = False,
    now_epoch: float | None = None,
) -> dict:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = Path(str(config.get("output_dir") or "/opt/var/lib/iptv-home-probe"))
    if enabled:
        if config.get("route_context") != "living-room-path-equivalent":
            if not confirm_living_room_path:
                raise RuntimeError(
                    "first verify router-origin traffic follows the living-room route, then use "
                    "--confirm-living-room-path"
                )
            config["route_context"] = "living-room-path-equivalent"
        if config.get("upload_enabled") is not True:
            raise RuntimeError("pair the Hong Kong receiver before activation")
        upload = json.loads((output / "upload-state.json").read_text(encoding="utf-8"))
        report = json.loads((output / "latest.json").read_text(encoding="utf-8"))
        count = int(upload.get("successful_uploads") or 0)
        first = parse_utc(str(upload.get("first_upload_utc") or ""))
        last = parse_utc(str(upload.get("last_upload_utc") or ""))
        if count < 4 or last - first < 18 * 3600:
            raise RuntimeError("shadow window incomplete: need 4 uploads spanning at least 18 hours")
        generated = parse_utc(str(report.get("generated_utc") or ""))
        if now_epoch - generated > 7 * 3600 or now_epoch - generated < -600:
            raise RuntimeError("latest local report is not fresh")
        if bool((report.get("summary") or {}).get("circuit_breaker_open")):
            raise RuntimeError("latest report opened the mass-failure circuit breaker")
    config["actionable"] = bool(enabled)
    atomic_json(config_path, config)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--confirm-living-room-path", action="store_true")
    args = parser.parse_args()
    try:
        set_actionable(
            Path(args.config),
            enabled=not args.off,
            confirm_living_room_path=args.confirm_living_room_path,
        )
        if args.off:
            print("HOME_PROBE_ACTIVATE off; uploads continue as non-actionable evidence")
        else:
            print("HOME_PROBE_ACTIVATE ready; the next completed report may inform the three-day rotation")
        return 0
    except Exception as exc:
        print(f"HOME_PROBE_ACTIVATE failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
