#!/opt/bin/python3
"""Pin the Hong Kong SSH host key and enable shadow-report uploads."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,}={0,2}$")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def public_key(config: dict) -> str:
    path = Path(str(config.get("ssh_private_key") or "/opt/etc/iptv-home-probe/id_ed25519"))
    return path.with_suffix(path.suffix + ".pub").read_text(encoding="ascii").strip()


def pair(config_path: Path, host: str, port: int, expected_fingerprint: str) -> dict:
    if not HOST_RE.fullmatch(host) or host.startswith("-") or ".." in host:
        raise RuntimeError("invalid SSH host")
    if port < 1 or port > 65535:
        raise RuntimeError("invalid SSH port")
    if not FINGERPRINT_RE.fullmatch(expected_fingerprint):
        raise RuntimeError("expected fingerprint must look like SHA256:...")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError("probe config is invalid")
    keyscan = str(config.get("ssh_keyscan") or "/opt/bin/ssh-keyscan")
    keygen = str(config.get("ssh_keygen") or "/opt/bin/ssh-keygen")
    scan = subprocess.run(
        [keyscan, "-T", "10", "-p", str(port), "-t", "ed25519", host],
        capture_output=True,
        text=True,
        timeout=20,
    )
    lines = [line for line in scan.stdout.splitlines() if line and not line.startswith("#")]
    if scan.returncode != 0 or not lines:
        raise RuntimeError("could not retrieve the VPS Ed25519 host key")
    candidate = ("\n".join(lines) + "\n").encode("ascii")
    check = subprocess.run(
        [keygen, "-lf", "-", "-E", "sha256"],
        input=candidate,
        capture_output=True,
        timeout=10,
    )
    fingerprints = {
        token.decode("ascii")
        for line in check.stdout.splitlines()
        for token in line.split()
        if token.startswith(b"SHA256:")
    }
    if expected_fingerprint not in fingerprints:
        raise RuntimeError(f"VPS host key mismatch; received {sorted(fingerprints)}")
    known_hosts = Path(str(config.get("ssh_known_hosts") or "/opt/etc/iptv-home-probe/known_hosts"))
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    temporary = known_hosts.with_name(f".{known_hosts.name}.{os.getpid()}.tmp")
    temporary.write_bytes(candidate)
    os.chmod(temporary, 0o600)
    os.replace(temporary, known_hosts)
    config.update({
        "upload_host": host,
        "upload_port": port,
        "upload_user": "iptv-home-probe",
        "upload_enabled": True,
        "actionable": False,
    })
    atomic_json(config_path, config)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    parser.add_argument("--show-key", action="store_true")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--fingerprint", default="")
    args = parser.parse_args()
    try:
        path = Path(args.config)
        config = json.loads(path.read_text(encoding="utf-8"))
        if args.show_key:
            print(public_key(config))
            return 0
        if not args.host or not args.fingerprint:
            raise RuntimeError("--host and --fingerprint are required")
        config = pair(path, args.host, args.port, args.fingerprint)
        print("HOME_PROBE_PAIR paired; uploads enabled in non-actionable shadow mode")
        print(f"probe_id={config['probe_id']}")
        return 0
    except Exception as exc:
        print(f"HOME_PROBE_PAIR failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
