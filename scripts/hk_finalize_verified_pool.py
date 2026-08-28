#!/usr/bin/env python3
"""Finalize the Hong Kong audit into a clean pool and station-only lineup.

After the full audit completes:
- harvest/candidates.jsonl contains only Hong Kong verified GOOD URLs, enriched
  with the objective quality evidence measured during the full audit
- non-GOOD URLs survive only as SHA256 tombstones so they cannot re-enter
- the production main lineup is rebuilt to 400 channels, Chinese-first and
  station-only: obvious VOD/loops/scenic/event-series content is rejected
- HD is preferred; verified 720p is used only when needed to reach 400
- sports are allowed with a soft cap, and one BBC-class service may be kept
- generic English channels are rejected unless they are sports/BBC
- tv-core.m3u and tv-easy.m3u are left untouched
- tv.m3u and tv-all.m3u are updated together

If a GitHub write token is configured on the HK host, the result is pushed.
Otherwise a local commit + snapshot is kept for later publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )


def quality_evidence(result: dict) -> dict:
    return {
        "checked_utc": str(result.get("checked_utc") or ""),
        "width": int(result.get("width") or 0),
        "height": int(result.get("height") or 0),
        "codec": str(result.get("codec") or ""),
        "field_order": str(result.get("field_order") or ""),
        "fps": float(result.get("fps") or 0),
        "bitrate_mbps": float(result.get("bitrate_mbps") or 0),
        "segment_ok": bool(result.get("segment_ok")),
        "segment_mbps": float(result.get("segment_mbps") or 0),
        "startup_s": float(result.get("startup_s") or 0),
        "transport": str(result.get("transport") or ""),
    }


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit-dir", default="/var/lib/iptv-hk-probe/all-url-audit")
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args()

    audit = Path(args.audit_dir)
    repo = Path(args.repo_root).resolve()
    summary_path = audit / "summary.json"
    results_path = audit / "results.jsonl"
    if not summary_path.exists() or not results_path.exists():
        raise SystemExit("audit results missing")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    total = int(summary.get("total_unique_urls") or 0)
    completed = int(summary.get("completed") or 0)
    if not total or completed != total:
        raise SystemExit(f"audit incomplete: completed={completed} total={total}")

    results = read_jsonl(results_path)
    by_url: dict[str, dict] = {}
    for row in results:
        url = str(row.get("url") or "").strip()
        if url:
            by_url[url] = row
    if len(by_url) != total:
        raise SystemExit(f"audit URL count mismatch: results={len(by_url)} total={total}")

    original = read_jsonl(repo / "harvest/candidates.jsonl")
    good_urls = {u for u, r in by_url.items() if str(r.get("status")) == "GOOD"}
    rejected_urls = set(by_url) - good_urls

    # Preserve every original label/provenance attached to each verified GOOD URL,
    # and embed measured quality. Future selection therefore happens after every
    # harvested URL has been measured, never before.
    active: list[dict] = []
    for source_row in original:
        url = str(source_row.get("url") or "").strip()
        if url not in good_urls:
            continue
        row = dict(source_row)
        row["hk_verified"] = quality_evidence(by_url[url])
        active.append(row)
    active.sort(
        key=lambda r: (
            str(r.get("group") or ""),
            str(r.get("name") or "").casefold(),
            str(r.get("url") or ""),
        )
    )

    tombstones = sorted(url_hash(u) for u in rejected_urls)
    (repo / "harvest/rejected-url-sha256.txt").write_text(
        "\n".join(tombstones) + ("\n" if tombstones else ""), encoding="utf-8"
    )
    write_jsonl(repo / "harvest/candidates.jsonl", active)

    pending = read_jsonl(repo / "harvest/pending.jsonl")
    resolved = set(by_url)
    pending = [r for r in pending if str(r.get("url") or "").strip() not in resolved]
    write_jsonl(repo / "harvest/pending.jsonl", pending)

    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "hong_kong_verified_active_pool",
        "production_modified": False,
        "audited_unique_urls": total,
        "verified_good_unique_urls": len(good_urls),
        "rejected_unique_urls": len(rejected_urls),
        "active_channel_url_entries": len(active),
        "pending_entries": len(pending),
        "quality_evidence_embedded": True,
        "policy": {
            "active_pool_contains_good_only": True,
            "degraded_removed_from_active_pool": True,
            "unknown_removed_from_active_pool": True,
            "rejected_repo_storage_is_hash_only": True,
            "future_harvest_rejects_known_tombstones": True,
            "selection_happens_after_full_hk_measurement": True,
            "curated_lineup_station_like_only": True,
            "curated_lineup_chinese_first": True,
            "curated_lineup_hd_first": True,
            "curated_lineup_target_channels": 400,
            "curated_lineup_sports_allowed": True,
            "curated_lineup_bbc_allowed": True,
            "generic_english_only_sports_or_bbc": True,
            "obvious_vod_loop_scenic_event_series_rejected": True,
        },
    }
    manifest_path = repo / "harvest/verified-manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Rebuild the production main list only after the GOOD-only pool above exists.
    # This builder preserves tv-core exactly and rewrites tv.m3u/tv-all.m3u together.
    builder = repo / "scripts/hk_rebuild_curated_station_only.py"
    if not builder.exists():
        raise SystemExit(f"station-only curated builder missing: {builder}")
    proc = subprocess.run(
        ["python3", str(builder)],
        cwd=str(repo),
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.returncode != 0:
        raise SystemExit(f"station-only curated builder failed rc={proc.returncode}: {(proc.stderr or '')[-500:]}")
    curated_published = True

    curated_manifest: dict = {}
    cp = repo / "harvest/curated-manifest.json"
    if cp.exists():
        try:
            curated_manifest = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            curated_manifest = {}
    result_channels = int(curated_manifest.get("channels") or curated_manifest.get("result_channels") or 0)
    payload["curated"] = {
        "published": curated_published,
        "stage": str(curated_manifest.get("stage") or ""),
        "result_channels": result_channels,
        "core_preserved": int(curated_manifest.get("core_preserved") or 0),
        "sports_channels": int(curated_manifest.get("sports_channels") or 0),
        "bbc_channels": int(curated_manifest.get("bbc_channels") or 0),
        "noncore_720_count": int(curated_manifest.get("noncore_720_count") or 0),
    }
    payload["production_modified"] = True
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(
        f"HK_POOL_FINALIZED audited={total} good_urls={len(good_urls)} rejected={len(rejected_urls)} "
        f"active_entries={len(active)} curated={result_channels}"
    )

    snapshot = Path("/var/lib/iptv-hk-probe/verified-pool-snapshot")
    snapshot.mkdir(parents=True, exist_ok=True)
    for rel in (
        "harvest/candidates.jsonl",
        "harvest/pending.jsonl",
        "harvest/rejected-url-sha256.txt",
        "harvest/verified-manifest.json",
        "harvest/curated-manifest.json",
        "tv.m3u",
        "tv-all.m3u",
    ):
        src = repo / rel
        if src.exists():
            shutil.copy2(src, snapshot / src.name)

    add_paths = [
        "harvest/candidates.jsonl",
        "harvest/pending.jsonl",
        "harvest/rejected-url-sha256.txt",
        "harvest/verified-manifest.json",
        "harvest/curated-manifest.json",
        "tv.m3u",
        "tv-all.m3u",
    ]
    git(repo, "add", *add_paths)
    staged = git(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("HK_POOL_FINALIZED no repository changes")
        return 0

    git(repo, "config", "user.name", "hk-iptv-probe")
    git(repo, "config", "user.email", "hk-iptv-probe@users.noreply.github.com")
    git(repo, "commit", "-m", "Publish Hong Kong verified station-only IPTV pool")

    token_file = Path("/etc/iptv-hk-probe.github-token")
    askpass = Path("/usr/local/libexec/iptv-hk-git-askpass")
    if token_file.exists() and token_file.stat().st_size > 0 and askpass.exists():
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = str(askpass)
        pushed = git(repo, "push", "origin", "HEAD:master", check=False, env=env)
        if pushed.returncode == 0:
            (snapshot / "NEEDS_PUBLISH").unlink(missing_ok=True)
            print("HK_POOL_FINALIZED pushed verified pool + station-only lineup")
            return 0
        print(f"HK_POOL_FINALIZED push failed: {(pushed.stderr or '')[-300:]}")

    (snapshot / "NEEDS_PUBLISH").touch()
    print(f"HK_POOL_FINALIZED saved local commit/snapshot; GitHub publish pending at {snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
