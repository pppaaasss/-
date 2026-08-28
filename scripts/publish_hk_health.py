#!/usr/bin/env python3
"""Publish a read-only Hong Kong health report to a non-production branch."""

from __future__ import annotations

import argparse
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_ROOT = "https://api.github.com"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def load_report(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    if len(raw) > 900_000:
        raise RuntimeError(f"health report is too large for safe upload: {len(raw)} bytes")
    report = json.loads(raw.decode("utf-8"))
    if report.get("production_modified") is not False:
        raise RuntimeError("refusing report that does not prove production_modified=false")
    policy = report.get("policy") or {}
    if policy.get("auto_replace_formal_routes") is not False:
        raise RuntimeError("refusing report that does not prove auto replacement is disabled")
    if not isinstance(report.get("results"), list):
        raise RuntimeError("health report has no results list")
    return report, raw


def validate_destination(repository: str, branch: str, destination: str) -> None:
    if not REPOSITORY_RE.fullmatch(repository):
        raise RuntimeError("invalid GitHub repository name")
    if branch != "health-monitor":
        raise RuntimeError("health reports may only target the health-monitor branch")
    if destination != "health/latest.json":
        raise RuntimeError("health reports may only target health/latest.json")


def build_put_payload(raw: bytes, branch: str, existing_sha: str | None, message: str) -> dict:
    payload = {
        "message": message,
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha
    return payload


def request_json(method: str, url: str, token: str, payload: dict | None = None, allow_404: bool = False):
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "HK-IPTV-Health-Publisher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail}") from exc


def publish(report_path: Path, repository: str, branch: str, destination: str, token_file: Path) -> int:
    validate_destination(repository, branch, destination)
    report, raw = load_report(report_path)
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if not token:
        print(f"HK_HEALTH_UPLOAD skipped: no token at {token_file}")
        return 0

    quoted_repo = urllib.parse.quote(repository, safe="/")
    quoted_path = urllib.parse.quote(destination, safe="/")
    base_url = f"{API_ROOT}/repos/{quoted_repo}/contents/{quoted_path}"
    current = request_json(
        "GET",
        f"{base_url}?{urllib.parse.urlencode({'ref': branch})}",
        token,
        allow_404=True,
    )
    existing_sha = str((current or {}).get("sha") or "") or None
    summary = report.get("summary") or {}
    generated = str(report.get("generated_utc") or "unknown-time")
    message = (
        f"Update Hong Kong health {generated} "
        f"GOOD={int(summary.get('good') or 0)} "
        f"DEGRADED={int(summary.get('degraded') or 0)} "
        f"UNKNOWN={int(summary.get('unknown') or 0)}"
    )
    payload = build_put_payload(raw, branch, existing_sha, message)
    result = request_json("PUT", base_url, token, payload=payload)
    commit_sha = str(((result or {}).get("commit") or {}).get("sha") or "")
    if not commit_sha:
        raise RuntimeError("GitHub health upload returned no commit SHA")
    print(f"HK_HEALTH_UPLOAD ok branch={branch} commit={commit_sha}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--repository", default="pppaaasss/-")
    parser.add_argument("--branch", default="health-monitor")
    parser.add_argument("--destination", default="health/latest.json")
    parser.add_argument("--token-file", default="/etc/iptv-hk-probe.github-token")
    args = parser.parse_args()
    return publish(
        Path(args.report),
        args.repository,
        args.branch,
        args.destination,
        Path(args.token_file),
    )


if __name__ == "__main__":
    raise SystemExit(main())
