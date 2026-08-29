#!/usr/bin/env python3
"""Validate and promote an already-built candidate set. Never runs on a schedule."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.viewer_locked_channels import ensure_locked_channels  # noqa: E402

CANDIDATE = ROOT / "candidate"
PRODUCTION = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def current_hashes() -> dict[str, str]:
    return {name: sha256(ROOT / name) for name in PRODUCTION}


def load_manifest() -> dict:
    path = CANDIDATE / "manifest.json"
    if not path.exists():
        raise SystemExit("candidate manifest missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("candidate manifest invalid")
    return data


def validate(manifest: dict, *, visual_confirmed: bool, audit_run_id: str) -> None:
    if not manifest.get("build_ok"):
        raise SystemExit("candidate build is not successful")
    if not manifest.get("production_hashes_unchanged"):
        raise SystemExit("candidate scan did not prove production hash invariance")
    missing = list(manifest.get("missing_core") or [])
    if missing:
        raise SystemExit("candidate missing required core: " + ", ".join(map(str, missing)))
    changed = list(manifest.get("changed_core") or [])
    streaks = manifest.get("core_streaks") or {}
    short = [key for key in changed if int(streaks.get(key, 0)) < 2]
    if short:
        raise SystemExit("core changes lack two consecutive scans: " + ", ".join(short))
    expected_run_id = str(manifest.get("candidate_run_id") or "").strip()
    if changed:
        if not manifest.get("frame_audit_ready"):
            absent = list(manifest.get("frame_audit_missing_changed_core") or [])
            detail = ": " + ", ".join(map(str, absent)) if absent else ""
            raise SystemExit("changed core routes lack a reviewable frame-audit artifact" + detail)
        if not visual_confirmed or not audit_run_id.strip():
            raise SystemExit("changed core routes require explicit visual artifact confirmation")
        if not expected_run_id or audit_run_id.strip() != expected_run_id:
            raise SystemExit("visual review run ID must match this candidate scan")
    expected = manifest.get("production_sha256_before") or {}
    actual = current_hashes()
    if expected != actual:
        raise SystemExit("production changed after candidate scan; candidate is stale")
    for name in PRODUCTION:
        path = CANDIDATE / name
        if not path.exists() or path.stat().st_size < 64:
            raise SystemExit(f"candidate file missing/empty: {name}")


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def promote(*, visual_confirmed: bool, audit_run_id: str) -> None:
    manifest = load_manifest()
    validate(manifest, visual_confirmed=visual_confirmed, audit_run_id=audit_run_id)
    previous_commit = git_head()
    before = current_hashes()
    for name in PRODUCTION:
        shutil.copy2(CANDIDATE / name, ROOT / name)
    # A reviewed full-catalogue promotion may not silently drop channels that
    # the viewer has confirmed at home.  Missing identities are restored while
    # existing routes remain untouched for confirmed-dead failover.
    ensure_locked_channels(ROOT)
    release = {
        "version": 1,
        "promoted_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "previous_commit": previous_commit,
        "candidate_base_commit": manifest.get("base_commit", ""),
        "candidate_generated_utc": manifest.get("generated_utc", ""),
        "candidate_audit_run_id": audit_run_id,
        "visual_review_confirmed": visual_confirmed,
        "changed_core": manifest.get("changed_core", []),
        "production_sha256_before_promotion": before,
        "production_sha256_after_promotion": current_hashes(),
    }
    (CANDIDATE / "last-production.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(release, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "promote"))
    parser.add_argument("--visual-confirmed", action="store_true")
    parser.add_argument("--audit-run-id", default="")
    args = parser.parse_args()
    manifest = load_manifest()
    if args.command == "validate":
        validate(manifest, visual_confirmed=args.visual_confirmed, audit_run_id=args.audit_run_id)
    else:
        promote(visual_confirmed=args.visual_confirmed, audit_run_id=args.audit_run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
