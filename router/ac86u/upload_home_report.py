#!/opt/bin/python3
"""Upload the latest home report over a pinned, forced-command SSH key."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


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
    os.replace(temporary, path)


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def upload(config_path: Path, report_path: Path | None = None) -> bool:
    config = load_object(config_path)
    if config.get("upload_enabled") is not True:
        print("HOME_PROBE_UPLOAD disabled; local shadow report retained")
        return False
    output_dir = Path(str(config.get("output_dir") or "/opt/var/lib/iptv-home-probe"))
    report_path = report_path or output_dir / "latest.json"
    raw = report_path.read_bytes()
    if len(raw) > 900_000:
        raise RuntimeError("latest report is too large")
    report = json.loads(raw.decode("utf-8"))
    if report.get("run_status") != "COMPLETED" or report.get("probe_id") != config.get("probe_id"):
        raise RuntimeError("latest report is incomplete or belongs to another probe")

    host = str(config.get("upload_host") or "").strip()
    if not HOST_RE.fullmatch(host) or host.startswith("-") or ".." in host:
        raise RuntimeError("upload_host is not configured safely")
    port = int(config.get("upload_port") or 22)
    if port < 1 or port > 65535:
        raise RuntimeError("upload_port is invalid")
    user = str(config.get("upload_user") or "iptv-home-probe")
    if user != "iptv-home-probe":
        raise RuntimeError("upload_user must remain the restricted receiver account")
    key = Path(str(config.get("ssh_private_key") or "/opt/etc/iptv-home-probe/id_ed25519"))
    known_hosts = Path(str(config.get("ssh_known_hosts") or "/opt/etc/iptv-home-probe/known_hosts"))
    if not key.is_file() or not known_hosts.is_file():
        raise RuntimeError("SSH key or pinned known_hosts is missing")
    if key.stat().st_mode & 0o077:
        raise RuntimeError("SSH private key permissions are too broad")

    ssh = str(config.get("ssh") or "/opt/bin/ssh")
    command = [
        ssh,
        "-T",
        "-p", str(port),
        "-i", str(key),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "ConnectTimeout=12",
        f"{user}@{host}",
    ]
    process = subprocess.run(command, input=raw, capture_output=True, timeout=45)
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"SSH receiver failed ({process.returncode}): {detail}")

    state_path = output_dir / "upload-state.json"
    try:
        state = load_object(state_path)
    except FileNotFoundError:
        state = {}
    generated = str(report.get("generated_utc") or "")
    if generated != state.get("last_report_generated_utc"):
        state["successful_uploads"] = int(state.get("successful_uploads") or 0) + 1
        state["first_upload_utc"] = str(state.get("first_upload_utc") or "") or utc_text()
    state["last_upload_utc"] = utc_text()
    state["last_report_generated_utc"] = generated
    atomic_json(state_path, state)
    print(process.stdout.decode("utf-8", "replace").strip() or "HOME_PROBE_UPLOAD accepted")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    try:
        upload(Path(args.config), Path(args.report) if args.report else None)
        return 0
    except Exception as exc:
        print(f"HOME_PROBE_UPLOAD failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
