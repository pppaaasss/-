#!/opt/bin/python3
"""Queue and push validated home reports directly to a GitHub report branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from .home_contract import validate_home_report_v2
except ImportError:  # Installed beside this file on the router.
    from home_contract import validate_home_report_v2  # type: ignore


REPORT_LIMIT = 4 * 1024 * 1024
REPOSITORY = "pppaaasss/-"
REPORT_BRANCH = "home-reports"
GITHUB_HOST = "ssh.github.com"
GITHUB_PORT = 443
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")


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


def atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def report_filename(report: dict, raw: bytes) -> str:
    stamp = re.sub(r"[^0-9TZ]", "", str(report["generated_utc"]))
    run_kind = str(report["run_kind"])
    digest = hashlib.sha256(raw).hexdigest()
    if not stamp or run_kind not in {"primary-0200", "recheck-1300"}:
        raise RuntimeError("report timestamp or run kind is invalid")
    return f"{stamp}-{run_kind}-{digest[:16]}.json"


def queue_report(config: dict, output_dir: Path, report_path: Path) -> Path:
    raw = report_path.read_bytes()
    if not raw or len(raw) > REPORT_LIMIT:
        raise RuntimeError("home report is empty or too large")
    try:
        report = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("home report is not valid UTF-8 JSON") from exc
    validate_home_report_v2(report, expected_probe_id=str(config.get("probe_id") or ""))
    queued = output_dir / "pending-reports" / report_filename(report, raw)
    if queued.exists():
        if queued.read_bytes() != raw:
            raise RuntimeError("queued report filename collision")
        return queued
    atomic_bytes(queued, raw)
    return queued


def validate_pending_reports(config: dict, pending: list[Path]) -> list[tuple[dict, bytes]]:
    """Revalidate every queued byte before it can enter the report branch."""
    validated: list[tuple[dict, bytes]] = []
    probe_id = str(config.get("probe_id") or "")
    for path in pending:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"queued report is not a regular file: {path.name}")
        raw = path.read_bytes()
        if not raw or len(raw) > REPORT_LIMIT:
            raise RuntimeError(f"queued report is empty or too large: {path.name}")
        try:
            report = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"queued report is not valid UTF-8 JSON: {path.name}") from exc
        validate_home_report_v2(report, expected_probe_id=probe_id)
        if path.name != report_filename(report, raw):
            raise RuntimeError(f"queued report filename does not match its hashed content: {path.name}")
        validated.append((report, raw))
    return validated


def _git(
    executable: str,
    args: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    timeout: int = 180,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        [executable, *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout or "git_failed").strip()[-800:]
        raise RuntimeError(f"git {' '.join(args[:2])} failed ({process.returncode}): {detail}")
    return process


def _production_transport(config: dict) -> tuple[str, dict[str, str]]:
    if config.get("protected_publishing_ready") is not True:
        raise RuntimeError("protected GitHub publishing is not ready; report remains queued")
    repository = str(config.get("github_repository") or "")
    branch = str(config.get("github_report_branch") or "")
    if repository != REPOSITORY or not REPOSITORY_RE.fullmatch(repository):
        raise RuntimeError("GitHub repository is not the pinned television repository")
    if branch != REPORT_BRANCH or not BRANCH_RE.fullmatch(branch):
        raise RuntimeError("GitHub report branch is not pinned safely")
    key = Path(str(config.get("github_deploy_private_key") or ""))
    known_hosts = Path(str(config.get("github_known_hosts") or ""))
    if not key.is_file() or not known_hosts.is_file():
        raise RuntimeError("GitHub deploy key or pinned known_hosts is missing")
    if key.stat().st_mode & 0o077:
        raise RuntimeError("GitHub deploy private key permissions are too broad")
    ssh = str(config.get("ssh") or "/opt/bin/ssh")
    ssh_command = shlex.join([
        ssh,
        "-i", str(key),
        "-p", str(GITHUB_PORT),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "ConnectTimeout=15",
    ])
    env = dict(os.environ)
    env.update({
        "GIT_SSH_COMMAND": ssh_command,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    })
    return f"ssh://git@{GITHUB_HOST}:{GITHUB_PORT}/{repository}.git", env


def _copy_pending(repo_dir: Path, probe_id: str, pending: list[Path]) -> None:
    inbox = repo_dir / "inbox"
    target_dir = inbox / probe_id
    for part in (inbox, target_dir):
        if part.is_symlink():
            raise RuntimeError("report branch contains an unsafe inbox symlink")
    target_dir.mkdir(parents=True, exist_ok=True)
    for source in pending:
        destination = target_dir / source.name
        if destination.is_symlink():
            raise RuntimeError("report branch contains an unsafe report symlink")
        raw = source.read_bytes()
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != raw:
                raise RuntimeError(f"remote report conflict: {source.name}")
            continue
        atomic_bytes(destination, raw)


def _push_once(
    config: dict,
    output_dir: Path,
    pending: list[Path],
    *,
    remote_url: str,
    transport_env: dict[str, str],
) -> None:
    git = str(config.get("git") or "/opt/bin/git")
    branch = str(config["github_report_branch"])
    probe_id = str(config["probe_id"])
    with tempfile.TemporaryDirectory(prefix="github-report-", dir=str(output_dir)) as temporary:
        repo_dir = Path(temporary) / "repo"
        _git(
            git,
            ["clone", "--quiet", "--depth", "1", "--single-branch", "--branch", branch, remote_url, str(repo_dir)],
            cwd=None,
            env=transport_env,
        )
        _git(git, ["config", "user.name", f"IPTV Home Probe {probe_id}"], cwd=repo_dir, env=transport_env)
        _git(git, ["config", "user.email", "iptv-home-probe@users.noreply.github.com"], cwd=repo_dir, env=transport_env)
        _copy_pending(repo_dir, probe_id, pending)
        relative = f"inbox/{probe_id}"
        _git(git, ["add", "--", relative], cwd=repo_dir, env=transport_env)
        changed = _git(git, ["diff", "--cached", "--quiet"], cwd=repo_dir, env=transport_env, check=False)
        if changed.returncode not in {0, 1}:
            raise RuntimeError("could not inspect staged home reports")
        if changed.returncode == 1:
            latest = pending[-1].name
            _git(git, ["commit", "--quiet", "-m", f"Home report {probe_id}: {latest}"], cwd=repo_dir, env=transport_env)
            _git(git, ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo_dir, env=transport_env)


def push(
    config_path: Path,
    report_path: Path | None = None,
    *,
    remote_url_override: str | None = None,
    transport_env_override: dict[str, str] | None = None,
) -> bool:
    config = load_object(config_path)
    output_dir = Path(str(config.get("output_dir") or "/opt/var/lib/iptv-home-probe"))
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_path or output_dir / "latest.json"
    queue_report(config, output_dir, report_path)
    pending = sorted((output_dir / "pending-reports").glob("*.json"))
    validated = validate_pending_reports(config, pending)
    state_path = output_dir / "github-state.json"
    try:
        state = load_object(state_path)
    except FileNotFoundError:
        state = {}
    state["pending_reports"] = len(pending)
    if config.get("github_push_enabled") is not True:
        atomic_json(state_path, state)
        print(f"HOME_GITHUB_PUSH disabled; queued={len(pending)}")
        return False

    error = ""
    try:
        if remote_url_override is None:
            remote_url, transport_env = _production_transport(config)
        else:
            remote_url = remote_url_override
            transport_env = dict(os.environ) if transport_env_override is None else dict(transport_env_override)
            transport_env.setdefault("GIT_TERMINAL_PROMPT", "0")
            transport_env.setdefault("LC_ALL", "C")
        for _attempt in range(3):
            try:
                _push_once(
                    config,
                    output_dir,
                    pending,
                    remote_url=remote_url,
                    transport_env=transport_env,
                )
                error = ""
                break
            except Exception as exc:
                error = str(exc)
    except Exception as exc:
        error = str(exc)
    if error:
        state["last_error"] = error[:800]
        state["last_attempt_utc"] = utc_text()
        state["pending_reports"] = len(pending)
        atomic_json(state_path, state)
        raise RuntimeError(error)

    acknowledged = len(validated)
    evidence = dict(state.get("successful_report_evidence") or {})
    for report, raw in validated:
        baseline = report["baseline"]
        evidence[report["generated_utc"]] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "safe": all(baseline.get(k) is True for k in ("home_network_ok", "github_reachable", "route_verified"))
                    and baseline.get("mass_failure_circuit_breaker") is False,
            "probe_id": report["probe_id"],
        }
    # A retried upload is the same observation, not another shadow report.
    evidence = dict(sorted(evidence.items())[-64:])
    state["successful_report_evidence"] = evidence
    state["successful_reports"] = len(evidence)
    state["successful_pushes"] = int(state.get("successful_pushes") or 0) + 1
    state["first_push_utc"] = str(state.get("first_push_utc") or "") or utc_text()
    state["last_push_utc"] = utc_text()
    state["last_report_generated_utc"] = str(validated[-1][0]["generated_utc"])
    state["last_report_sha256"] = hashlib.sha256(validated[-1][1]).hexdigest()
    state["pending_reports"] = 0
    state.pop("last_error", None)
    atomic_json(state_path, state)
    for path in pending:
        path.unlink()
    print(f"HOME_GITHUB_PUSH accepted={acknowledged} pending=0 branch={config['github_report_branch']}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/etc/iptv-home-probe.json")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    try:
        push(Path(args.config), Path(args.report) if args.report else None)
        return 0
    except Exception as exc:
        print(f"HOME_GITHUB_PUSH failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
