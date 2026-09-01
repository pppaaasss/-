#!/opt/bin/python3
"""Pin GitHub's SSH host key and enable the repository-scoped deploy key."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


GITHUB_HOST = "ssh.github.com"
GITHUB_PORT = 443
REPOSITORY = "pppaaasss/-"
REPORT_BRANCH = "home-reports"
PUBLISHER_CONFIG_SCHEMA = "iptv-home-publisher-config/v1"
PUBLISHER_CONFIG_URL = "https://raw.githubusercontent.com/pppaaasss/-/master/config/home-publisher.json"
MASTER_RULES_API = "https://api.github.com/repos/pppaaasss/-/rules/branches/master"
REMOTE_JSON_LIMIT = 128 * 1024
GITHUB_ED25519_FINGERPRINT = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
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


def load_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("probe config is invalid")
    return value


def public_key(config: dict) -> str:
    private = Path(str(config.get("github_deploy_private_key") or ""))
    public = private.with_suffix(private.suffix + ".pub")
    value = public.read_text(encoding="ascii").strip()
    if not value.startswith("ssh-ed25519 "):
        raise RuntimeError("GitHub deploy public key is not Ed25519")
    return value


def validate_destination(config: dict, *, require_ready: bool = True) -> None:
    if config.get("github_repository") != REPOSITORY:
        raise RuntimeError("GitHub destination is not the pinned television repository")
    if config.get("github_report_branch") != REPORT_BRANCH:
        raise RuntimeError("GitHub destination is not the isolated home report branch")
    if require_ready and config.get("protected_publishing_ready") is not True:
        raise RuntimeError("protected GitHub publishing is not ready")


def fetch_json(url: str) -> tuple[object, bytes]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "AC86U-IPTV-Home-Pair/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(REMOTE_JSON_LIMIT + 1)
    if not raw or len(raw) > REMOTE_JSON_LIMIT:
        raise RuntimeError("GitHub readiness response is empty or too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("GitHub readiness response is not valid JSON") from exc
    return value, raw


def validate_master_rules(rules: object) -> int:
    if not isinstance(rules, list):
        raise RuntimeError("GitHub master rules response is not a list")
    by_ruleset: dict[int, dict[str, dict]] = {}
    for value in rules:
        if not isinstance(value, dict):
            raise RuntimeError("GitHub master rule is not an object")
        ruleset_id = value.get("ruleset_id")
        rule_type = str(value.get("type") or "")
        if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int) or ruleset_id <= 0:
            raise RuntimeError("GitHub master rule lacks a valid ruleset ID")
        if value.get("ruleset_source_type") != "Repository" or value.get("ruleset_source") != REPOSITORY:
            continue
        by_ruleset.setdefault(ruleset_id, {})[rule_type] = value

    for ruleset_id, active in by_ruleset.items():
        if not {"pull_request", "deletion", "non_fast_forward"}.issubset(active):
            continue
        parameters = active["pull_request"].get("parameters")
        if not isinstance(parameters, dict):
            continue
        if parameters.get("required_approving_review_count") != 0:
            continue
        if parameters.get("require_code_owner_review") is not False:
            continue
        if parameters.get("require_last_push_approval") is not False:
            continue
        if parameters.get("required_review_thread_resolution") is not False:
            continue
        methods = parameters.get("allowed_merge_methods")
        if not isinstance(methods, list) or "squash" not in methods:
            continue
        return ruleset_id
    raise RuntimeError(
        "GitHub master lacks one active no-human-approval ruleset with PR, deletion, and force-push protection"
    )


def validate_protected_publisher(publisher: dict, rules: object, config: dict) -> int:
    if publisher.get("schema") != PUBLISHER_CONFIG_SCHEMA or publisher.get("enabled") is not True:
        raise RuntimeError("protected home publisher is not enabled on master")
    if publisher.get("repository") != REPOSITORY or publisher.get("report_branch") != REPORT_BRANCH:
        raise RuntimeError("protected home publisher destination does not match")
    if publisher.get("expected_probe_id") != config.get("probe_id"):
        raise RuntimeError("protected home publisher is not assigned to this probe")
    if publisher.get("branch_protection_required") is not True:
        raise RuntimeError("protected home publisher does not require branch protection")
    return validate_master_rules(rules)


def verify_protected_publisher(config: dict) -> tuple[str, str, int]:
    publisher, raw = fetch_json(PUBLISHER_CONFIG_URL)
    rules, rules_raw = fetch_json(MASTER_RULES_API)
    if not isinstance(publisher, dict):
        raise RuntimeError("GitHub publisher configuration is not an object")
    ruleset_id = validate_protected_publisher(publisher, rules, config)
    return (
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(rules_raw).hexdigest(),
        ruleset_id,
    )


def pin_host_key(config: dict, expected_fingerprint: str = GITHUB_ED25519_FINGERPRINT) -> Path:
    if not FINGERPRINT_RE.fullmatch(expected_fingerprint):
        raise RuntimeError("expected fingerprint must look like SHA256:...")
    if expected_fingerprint != GITHUB_ED25519_FINGERPRINT:
        raise RuntimeError("refusing a fingerprint that differs from GitHub's pinned Ed25519 key")
    keyscan = str(config.get("ssh_keyscan") or "/opt/bin/ssh-keyscan")
    keygen = str(config.get("ssh_keygen") or "/opt/bin/ssh-keygen")
    scan = subprocess.run(
        [keyscan, "-T", "10", "-p", str(GITHUB_PORT), "-t", "ed25519", GITHUB_HOST],
        capture_output=True,
        timeout=20,
    )
    lines = [line for line in scan.stdout.splitlines() if line and not line.startswith(b"#")]
    if scan.returncode != 0 or not lines:
        raise RuntimeError("could not retrieve GitHub's Ed25519 host key")
    prefix = f"[{GITHUB_HOST}]:{GITHUB_PORT} ssh-ed25519 ".encode("ascii")
    if any(not line.startswith(prefix) for line in lines):
        raise RuntimeError("GitHub key scan returned an unexpected host or key type")
    candidate = b"\n".join(lines) + b"\n"
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
    if check.returncode != 0 or fingerprints != {expected_fingerprint}:
        raise RuntimeError(f"GitHub host key mismatch; received {sorted(fingerprints)}")
    known_hosts = Path(str(config.get("github_known_hosts") or ""))
    if not known_hosts.name:
        raise RuntimeError("GitHub known_hosts path is missing")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    temporary = known_hosts.with_name(f".{known_hosts.name}.{os.getpid()}.tmp")
    temporary.write_bytes(candidate)
    os.chmod(temporary, 0o600)
    os.replace(temporary, known_hosts)
    return known_hosts


def authenticate(config: dict, known_hosts: Path) -> None:
    key = Path(str(config.get("github_deploy_private_key") or ""))
    if not key.is_file() or key.stat().st_mode & 0o077:
        raise RuntimeError("GitHub deploy private key is missing or has unsafe permissions")
    ssh = str(config.get("ssh") or "/opt/bin/ssh")
    process = subprocess.run(
        [
            ssh,
            "-T",
            "-p", str(GITHUB_PORT),
            "-i", str(key),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=15",
            f"git@{GITHUB_HOST}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    detail = f"{process.stdout}\n{process.stderr}"
    # GitHub intentionally returns exit code 1 because it provides no shell.
    if process.returncode not in {0, 1} or "successfully authenticated" not in detail:
        raise RuntimeError(f"GitHub deploy key authentication failed: {detail.strip()[-500:]}")


def enable(config_path: Path) -> dict:
    config = load_config(config_path)
    validate_destination(config, require_ready=False)
    publisher_sha, rules_sha, ruleset_id = verify_protected_publisher(config)
    known_hosts = pin_host_key(config)
    authenticate(config, known_hosts)
    config["github_push_enabled"] = True
    config["protected_publishing_ready"] = True
    config["publisher_config_sha256"] = publisher_sha
    config["master_rules_sha256"] = rules_sha
    config["master_ruleset_id"] = ruleset_id
    config["publisher_checked_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    config["actionable"] = False
    atomic_json(config_path, config)
    return config


def disable(config_path: Path) -> dict:
    config = load_config(config_path)
    config["github_push_enabled"] = False
    config["protected_publishing_ready"] = False
    config["actionable"] = False
    atomic_json(config_path, config)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--show-key", action="store_true")
    action.add_argument("--pin-host", action="store_true")
    action.add_argument("--enable", action="store_true")
    action.add_argument("--off", action="store_true")
    args = parser.parse_args()
    try:
        path = Path(args.config)
        config = load_config(path)
        if args.show_key:
            print(public_key(config))
        elif args.pin_host:
            pin_host_key(config)
            print(f"HOME_GITHUB_PAIR host pinned: {GITHUB_ED25519_FINGERPRINT}")
        elif args.enable:
            value = enable(path)
            print(f"HOME_GITHUB_PAIR enabled repository={value['github_repository']} branch={value['github_report_branch']}")
        else:
            disable(path)
            print("HOME_GITHUB_PAIR disabled; local report queue retained")
        return 0
    except Exception as exc:
        print(f"HOME_GITHUB_PAIR failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
