#!/usr/bin/env python3
"""Finalize a full Hong Kong URL audit into a clean verified spare pool.

Active repository semantics after migration:
- harvest/candidates.jsonl: ONLY URLs verified GOOD from Hong Kong
- harvest/pending.jsonl: newly harvested, never-tested URLs
- harvest/rejected-url-sha256.txt: fingerprints of non-GOOD URLs so dead routes
  cannot be silently reintroduced by the next GitHub text harvest

The full rejected URL/result history remains on the Hong Kong host under
/var/lib/iptv-hk-probe/all-url-audit; repository tombstones contain hashes only.
Production tv*.m3u files are never modified here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit-dir", default="/var/lib/iptv-hk-probe/all-url-audit")
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args()

    audit = Path(args.audit_dir)
    repo = Path(args.repo_root)
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

    # Preserve every original channel label/provenance attached to a GOOD URL.
    active = [r for r in original if str(r.get("url") or "").strip() in good_urls]
    active.sort(key=lambda r: (str(r.get("group") or ""), str(r.get("name") or "").casefold(), str(r.get("url") or "")))

    # The repository remembers rejected routes only by fingerprint. Full failure
    # evidence stays on the HK probe host, keeping the repo's active pool clean.
    tombstones = sorted(url_hash(u) for u in rejected_urls)
    (repo / "harvest/rejected-url-sha256.txt").write_text("\n".join(tombstones) + ("\n" if tombstones else ""), encoding="utf-8")
    write_jsonl(repo / "harvest/candidates.jsonl", active)

    # Anything in pending that was part of this completed audit is now resolved.
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
        "policy": {
            "active_pool_contains_good_only": True,
            "degraded_removed_from_active_pool": True,
            "unknown_removed_from_active_pool": True,
            "rejected_repo_storage_is_hash_only": True,
            "future_harvest_rejects_known_tombstones": True,
            "formal_playlists_modified": False,
        },
    }
    (repo / "harvest/verified-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"HK_POOL_FINALIZED audited={total} good_urls={len(good_urls)} "
        f"rejected={len(rejected_urls)} active_entries={len(active)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
