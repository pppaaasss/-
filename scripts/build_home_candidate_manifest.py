#!/usr/bin/env python3
"""Build an unverified CCTV/satellite candidate manifest for the home router.

This command performs no network or stream probe.  Its output is intentionally
marked as unverified and production-ineligible; AC86U is the only qualifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router.ac86u.home_contract import (  # noqa: E402
    CANDIDATE_SCHEMA,
    ContractError,
    make_candidate,
    object_sha256,
    station_key,
    utc_text,
    validate_candidate_manifest,
)
from scripts.home_route_policy import rejected_hosts as feedback_rejected_hosts  # noqa: E402
from scripts.home_route_policy import rejected_urls as feedback_rejected_urls  # noqa: E402


DEFAULT_FORMAL_URL = "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u"
INDEX_SCHEMA = "iptv-home-candidate-index/v1"


def jsonl_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{path}:{number}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ContractError(f"{path}:{number}: expected an object")
        rows.append(value)
    return rows


def playlist_entries(raw: bytes) -> list[tuple[str, str]]:
    lines = raw.decode("utf-8", "ignore").splitlines()
    entries: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF") or "," not in line:
            continue
        name = line.rsplit(",", 1)[-1].strip()
        cursor = index + 1
        while cursor < len(lines) and (not lines[cursor].strip() or lines[cursor].lstrip().startswith("#")):
            cursor += 1
        if cursor < len(lines) and lines[cursor].strip().startswith(("http://", "https://")):
            entries.append((name, lines[cursor].strip()))
    return entries


def build_manifest(
    *,
    discovery_rows: list[dict],
    formal_bytes: bytes,
    formal_url: str,
    source_revision: str,
    generated_utc: str,
    rejected_urls: set[str] | None = None,
    rejected_hosts: set[str] | None = None,
) -> tuple[dict, dict]:
    rejected_urls = set(rejected_urls or ())
    rejected_hosts = {value.casefold() for value in (rejected_hosts or ())}
    formal_entries = playlist_entries(formal_bytes)
    current: dict[str, str] = {}
    for name, url in formal_entries:
        key = station_key(name)
        if key is None:
            continue
        if key in current and current[key] != url:
            raise ContractError(f"formal playlist has conflicting routes for {key}")
        current[key] = url
    if not current:
        raise ContractError("formal playlist has no CCTV/provincial-satellite routes")

    candidates: dict[str, dict] = {}
    rejected = {
        "outside_scope": 0,
        "current_route": 0,
        "invalid": 0,
        "home_feedback": 0,
    }
    for source in discovery_rows:
        try:
            row = make_candidate(source)
        except ContractError as exc:
            if "outside" in str(exc):
                rejected["outside_scope"] += 1
            else:
                rejected["invalid"] += 1
            continue
        key = str(row["channel_key"])
        if key not in current:
            rejected["outside_scope"] += 1
            continue
        if row["url"] == current[key]:
            rejected["current_route"] += 1
            continue
        host = (urlsplit(str(row["url"])).hostname or "").casefold()
        if row["url"] in rejected_urls or (host and host in rejected_hosts):
            rejected["home_feedback"] += 1
            continue
        identity = str(row["candidate_id"])
        previous = candidates.get(identity)
        if previous is None:
            candidates[identity] = row
        else:
            previous["sources"] = sorted(set(previous["sources"]) | set(row["sources"]))

    ordered = sorted(candidates.values(), key=lambda item: (str(item["channel_key"]), str(item["candidate_id"])))
    manifest = {
        "schema": CANDIDATE_SCHEMA,
        "generated_utc": generated_utc,
        "source_revision": source_revision,
        "scope": ["cctv", "provincial_satellite"],
        "formal_playlist": {
            "url": formal_url,
            "sha256": hashlib.sha256(formal_bytes).hexdigest(),
            "channel_count": len(current),
        },
        "cloud_stream_probe_performed": False,
        "home_verified": False,
        "production_eligible": False,
        "candidate_count": len(ordered),
        "candidate_set_sha256": object_sha256(ordered),
        "candidates": ordered,
    }
    validate_candidate_manifest(manifest)
    summary = {
        "formal_channels": len(current),
        "discovery_rows": len(discovery_rows),
        "candidate_count": len(ordered),
        "rejected": rejected,
    }
    return manifest, summary


def build_incremental_manifest(
    full_manifest: dict,
    previous_index: dict | None,
    *,
    bootstrap_if_missing: bool = True,
) -> tuple[dict, dict, dict]:
    """Return only new/changed rows and the current snapshot index.

    The index intentionally contains only the previous run's current snapshot.
    A route that disappears and later returns is therefore emitted again.  On
    first deployment the snapshot is bootstrapped with an empty delta so the
    router never receives the entire historical pool at once.
    """
    if previous_index is not None:
        if previous_index.get("schema") != INDEX_SCHEMA:
            raise ContractError("unsupported home candidate index schema")
        old = previous_index.get("candidate_digests")
        if not isinstance(old, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or len(value) != 64
            for key, value in old.items()
        ):
            raise ContractError("home candidate index is invalid")
        previous_digests = dict(old)
    else:
        previous_digests = {}

    current_digests = {
        str(row["candidate_id"]): object_sha256(row)
        for row in full_manifest["candidates"]
    }
    bootstrap = previous_index is None and bootstrap_if_missing
    changed = [] if bootstrap else [
        row for row in full_manifest["candidates"]
        if previous_digests.get(str(row["candidate_id"])) != current_digests[str(row["candidate_id"])]
    ]
    delta = dict(full_manifest)
    delta["candidates"] = changed
    delta["candidate_count"] = len(changed)
    delta["candidate_set_sha256"] = object_sha256(changed)
    validate_candidate_manifest(delta)
    current_ids = set(current_digests)
    index = {
        "schema": INDEX_SCHEMA,
        "generated_utc": full_manifest["generated_utc"],
        "source_revision": full_manifest["source_revision"],
        "candidate_count": len(current_digests),
        "candidate_digests": dict(sorted(current_digests.items())),
        "candidate_snapshot_sha256": object_sha256(current_digests),
    }
    summary = {
        "bootstrap": bootstrap,
        "current_candidates": len(current_digests),
        "new_or_changed": len(changed),
        "disappeared": len(set(previous_digests) - current_ids),
    }
    return delta, index, summary


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="harvest/pending.jsonl")
    parser.add_argument("--formal", default="tv-core.m3u")
    parser.add_argument("--formal-url", default=DEFAULT_FORMAL_URL)
    parser.add_argument("--output", default="harvest/home-candidates.json")
    parser.add_argument("--index", default="harvest/home-candidate-index.json")
    parser.add_argument("--feedback", default="config/home-route-feedback.json")
    parser.add_argument("--no-bootstrap", action="store_true")
    parser.add_argument("--source-revision", default=os.environ.get("GITHUB_SHA") or "working-tree")
    args = parser.parse_args()

    source_path = Path(args.input)
    formal_path = Path(args.formal)
    output_path = Path(args.output)
    index_path = Path(args.index)
    previous_index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else None
    manifest, summary = build_manifest(
        discovery_rows=jsonl_rows(source_path),
        formal_bytes=formal_path.read_bytes(),
        formal_url=str(args.formal_url),
        source_revision=str(args.source_revision),
        generated_utc=utc_text(),
        rejected_urls=feedback_rejected_urls(Path(args.feedback)),
        rejected_hosts=feedback_rejected_hosts(Path(args.feedback), 2),
    )
    manifest, index, incremental = build_incremental_manifest(
        manifest,
        previous_index,
        bootstrap_if_missing=not args.no_bootstrap,
    )
    # Publish the delta before advancing the index.  A crash can therefore
    # repeat safe work, but can never silently skip a new candidate.
    atomic_json(output_path, manifest)
    atomic_json(index_path, index)
    print(
        "HOME_CANDIDATE_MANIFEST "
        f"formal={summary['formal_channels']} discovery={summary['discovery_rows']} "
        f"current={incremental['current_candidates']} delta={incremental['new_or_changed']} "
        f"bootstrap={int(incremental['bootstrap'])} disappeared={incremental['disappeared']} "
        f"rejected={json.dumps(summary['rejected'], sort_keys=True)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
