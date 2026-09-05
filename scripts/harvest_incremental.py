#!/usr/bin/env python3
"""Fetch upstream IPTV text for legacy or home-first candidate discovery.

GitHub performs text discovery only and never promotes a route.  ``--home-only``
writes a current text snapshot for the AC86U candidate builder without reading
Hong Kong probe results, active pools, or rejection tombstones.

Repository pool semantics:
- harvest/candidates.jsonl: Hong Kong verified GOOD active pool
- harvest/pending.jsonl: newly discovered URLs awaiting Hong Kong verification
- harvest/rejected-url-sha256.txt: tombstones for non-GOOD URLs
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_playlist as bp  # noqa: E402
import harvest_sources as hs  # noqa: E402


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


def read_tombstones(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {x.strip().lower() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


def normalize_rows(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    provenance: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        name = hs.clean_name(str(row.get("name") or ""))
        url = str(row.get("url") or "").strip()
        if not name or not hs.valid_url(url):
            continue
        key = (name, url)
        sources = row.get("sources") if isinstance(row.get("sources"), list) else []
        if row.get("source"):
            sources = [*sources, str(row.get("source"))]
        provenance[key].update(str(x) for x in sources if x)
        if key not in merged:
            merged[key] = {
                "name": name,
                "group": hs.clean_name(str(row.get("group") or "")),
                "url": url,
                "options": str(row.get("options") or ""),
            }
    out: list[dict] = []
    for key, row in merged.items():
        x = dict(row)
        x["sources"] = sorted(provenance[key])
        out.append(x)
    out.sort(key=lambda x: (x["group"], x["name"].casefold(), x["url"]))
    return out


def atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="harvest")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--home-only", action="store_true")
    ap.add_argument("--home-discovery-output", default="")
    args = ap.parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    source_defs = list(bp.SOURCES)
    source_stats: list[dict] = []
    failures: list[dict] = []

    def one(item):
        group, url, _flag = item
        started = time.monotonic()
        try:
            text, final = hs.fetch_text(url)
            rows = hs.parse_source(text, group, url)
            stat = {
                "group": group, "source": url, "final_url": final, "ok": True,
                "entries": len(rows), "elapsed_s": round(time.monotonic() - started, 3),
            }
            return rows, stat
        except Exception as exc:
            stat = {
                "group": group, "source": url, "ok": False, "entries": 0,
                "elapsed_s": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}:{str(exc)[:240]}",
            }
            return [], stat

    raw: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for got, stat in ex.map(one, source_defs):
            raw.extend(got)
            source_stats.append(stat)
            if not stat.get("ok"):
                failures.append(stat)

    for extra in getattr(bp, "EXTRAS", []):
        if not isinstance(extra, (tuple, list)) or len(extra) < 3:
            continue
        name, group, url = str(extra[0]), str(extra[1]), str(extra[2])
        clean_url, options = hs.split_url_options(url)
        if hs.clean_name(name) and hs.valid_url(clean_url):
            raw.append({
                "name": hs.clean_name(name), "group": hs.clean_name(group),
                "url": clean_url, "options": options, "source": "curated:EXTRAS",
            })

    discovered = normalize_rows(raw)
    if args.home_only:
        if not args.home_discovery_output:
            raise SystemExit("--home-only requires --home-discovery-output")
        destination = Path(args.home_discovery_output)
        atomic_jsonl(destination, discovered)
        raw_urls = {str(row.get("url") or "").strip() for row in discovered}
        manifest = {
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": "github_home_text_discovery_only",
            "stream_probe_performed": False,
            "production_modified": False,
            "source_definitions": len(source_defs),
            "sources_fetched_ok": sum(bool(item.get("ok")) for item in source_stats),
            "sources_failed": len(failures),
            "discovered_entries": len(discovered),
            "discovered_unique_urls": len(raw_urls),
            "policy": {
                "github_only_discovers_text": True,
                "github_never_qualifies_routes": True,
                "home_probe_is_the_only_health_authority": True,
                "hong_kong_evidence_consulted": False,
                "hong_kong_tombstones_consulted": False,
            },
            "sources": sorted(source_stats, key=lambda item: (str(item.get("group")), str(item.get("source")))),
            "failures": failures,
        }
        atomic_json(out / "home-discovery-manifest.json", manifest)
        print(
            f"HOME_TEXT_DISCOVERY urls={len(raw_urls)} entries={len(discovered)} "
            f"sources_ok={manifest['sources_fetched_ok']} sources_failed={len(failures)}"
        )
        return 0 if len(discovered) >= 100 else 2

    active = normalize_rows(read_jsonl(out / "candidates.jsonl"))
    existing_pending = normalize_rows(read_jsonl(out / "pending.jsonl"))
    tombstones = read_tombstones(out / "rejected-url-sha256.txt")
    active_urls = {str(r.get("url") or "").strip() for r in active}
    old_pending_urls = {str(r.get("url") or "").strip() for r in existing_pending}

    eligible_new = [
        r for r in discovered
        if str(r.get("url") or "").strip() not in active_urls
        and url_hash(str(r.get("url") or "")) not in tombstones
    ]
    pending = normalize_rows([*existing_pending, *eligible_new])
    pending_urls = {str(r.get("url") or "").strip() for r in pending}
    new_unique = pending_urls - old_pending_urls

    (out / "pending.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in pending),
        encoding="utf-8",
    )

    raw_urls = {str(r.get("url") or "").strip() for r in discovered}
    manifest = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "github_incremental_discovery_only",
        "stream_probe_performed": False,
        "production_modified": False,
        "source_definitions": len(source_defs),
        "sources_fetched_ok": sum(bool(x.get("ok")) for x in source_stats),
        "sources_failed": len(failures),
        "raw_discovered_entries": len(discovered),
        "raw_discovered_unique_urls": len(raw_urls),
        "active_verified_entries": len(active),
        "active_verified_unique_urls": len(active_urls),
        "rejected_tombstones": len(tombstones),
        "pending_entries": len(pending),
        "pending_unique_urls": len(pending_urls),
        "new_unique_urls_this_run": len(new_unique),
        "policy": {
            "github_only_discovers_text": True,
            "github_never_promotes_active_routes": True,
            "known_rejected_urls_never_reenter_pending": True,
            "active_pool_is_not_overwritten_by_harvest": True,
            "hong_kong_is_the_only_verification_stage": True,
        },
        "sources": sorted(source_stats, key=lambda x: (str(x.get("group")), str(x.get("source")))),
        "failures": failures,
    }
    (out / "incremental-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"INCREMENTAL_HARVEST raw_urls={len(raw_urls)} active_urls={len(active_urls)} "
        f"tombstones={len(tombstones)} pending_urls={len(pending_urls)} new_urls={len(new_unique)}"
    )
    return 0 if len(discovered) >= 100 else 2


if __name__ == "__main__":
    raise SystemExit(main())
