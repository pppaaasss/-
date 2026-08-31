#!/usr/bin/env python3
"""Receive one home-probe report through an SSH forced command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from .home_probe_report import (
        MAX_REPORT_BYTES,
        load_home_report_bytes,
        parse_utc,
        report_fingerprint,
    )
except ImportError:  # Installed beside the validator on the VPS.
    from home_probe_report import (  # type: ignore
        MAX_REPORT_BYTES,
        load_home_report_bytes,
        parse_utc,
        report_fingerprint,
    )


def utc_text(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


def receive(
    raw: bytes,
    *,
    expected_probe_id: str,
    output_dir: Path,
    now_epoch: float | None = None,
    history_limit: int = 40,
) -> tuple[str, Path]:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    report = load_home_report_bytes(
        raw,
        expected_probe_id=expected_probe_id,
        now_epoch=now_epoch,
        max_age_hours=12,
    )
    latest = output_dir / "latest.json"
    incoming_time = parse_utc(str(report["generated_utc"]))
    incoming_fingerprint = report_fingerprint(report)
    if latest.exists():
        try:
            current = json.loads(latest.read_text(encoding="utf-8"))
            current_time = parse_utc(str(current.get("generated_utc") or ""))
        except Exception as exc:
            raise RuntimeError("existing home report is corrupt; refusing to overwrite it") from exc
        if incoming_time < current_time:
            raise RuntimeError("refusing an older home report")
        if incoming_time == current_time:
            if report_fingerprint(current) == incoming_fingerprint:
                return "duplicate", latest
            raise RuntimeError("same-timestamp home report content changed")

    received = utc_text(now_epoch)
    report["transport"] = {
        "via": "ssh-forced-command",
        "receiver_validated": True,
        "received_utc": received,
        "report_sha256": incoming_fingerprint,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    history = output_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = str(report["generated_utc"]).replace("-", "").replace(":", "")
    archive = history / f"{stamp}-{incoming_fingerprint[:12]}.json"
    atomic_json(archive, report)
    atomic_json(latest, report)

    files = sorted(history.glob("*.json"), key=lambda path: path.name, reverse=True)
    for stale in files[max(1, int(history_limit)) :]:
        stale.unlink()
    return "accepted", latest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-probe-id", required=True)
    parser.add_argument("--output-dir", default="/var/lib/iptv-hk-probe/home")
    parser.add_argument("--history-limit", type=int, default=40)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise RuntimeError("home report is too large")
        status, destination = receive(
            raw,
            expected_probe_id=args.expected_probe_id,
            output_dir=Path(args.output_dir),
            history_limit=args.history_limit,
        )
        print(f"HOME_PROBE_RECEIVE {status} path={destination}")
        return 0
    except Exception as exc:
        print(f"HOME_PROBE_RECEIVE rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
