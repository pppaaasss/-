#!/usr/bin/env python3
"""Candidate-only hard rejection for routes disproved by home playback.

The living-room player is authoritative for real usability. GitHub runner results
remain useful for discovery/codec/resolution diagnostics, but a URL explicitly
reported as unplayable, buffering forever, or lower-resolution at home must not
be selected again by future candidate builds.

This module is applied only by scripts/run_candidate.py. It never edits or
publishes the four production playlists.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEEDBACK_PATH = ROOT / "config" / "home-route-feedback.json"


def normalize_url(value: str) -> str:
    """Normalize only harmless whitespace; preserve query tokens and identity."""
    return str(value or "").strip()


def load_feedback(path: Path | str | None = None) -> dict[str, Any]:
    feedback_path = Path(path) if path is not None else DEFAULT_FEEDBACK_PATH
    if not feedback_path.exists():
        return {"good": {}, "bad": {}}
    data = json.loads(feedback_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("home route feedback must be a JSON object")
    data.setdefault("good", {})
    data.setdefault("bad", {})
    return data


def rejected_urls(path: Path | str | None = None) -> set[str]:
    data = load_feedback(path)
    rejected: set[str] = set()
    for entries in (data.get("bad") or {}).values():
        for item in entries or []:
            value = item.get("url") if isinstance(item, dict) else item
            url = normalize_url(value)
            if url:
                rejected.add(url)
    return rejected


def rejected_hosts(
    path: Path | str | None = None,
    minimum_distinct_urls: int = 2,
) -> set[str]:
    """Return relay hosts repeatedly disproved by living-room playback.

    One rejected URL is kept as an exact veto only.  Multiple distinct failed
    URLs on the same host indicate a home-network/player incompatibility, so
    automatic selectors must stop moving other channels onto that relay.
    """
    if minimum_distinct_urls <= 0:
        return set()
    by_host: dict[str, set[str]] = {}
    for url in rejected_urls(path):
        host = (urlsplit(url).hostname or "").casefold()
        if host:
            by_host.setdefault(host, set()).add(url)
    return {
        host
        for host, urls in by_host.items()
        if len(urls) >= minimum_distinct_urls
    }


def apply(bp: ModuleType, feedback_path: Path | str | None = None) -> None:
    """Hard-fail user-rejected URLs before candidate ranking/publication."""
    bad_urls = rejected_urls(feedback_path)
    if not bad_urls:
        return

    def is_rejected(channel) -> bool:
        return normalize_url(getattr(channel, "url", "")) in bad_urls

    original_probe_channel = bp.probe_channel
    original_actual_height = bp.actual_height
    original_is_stable = bp.is_stable
    original_is_core_acceptable = bp.is_core_acceptable
    original_measured_score = bp.measured_score

    def probe_channel(channel) -> dict:
        if is_rejected(channel):
            return {
                "ok": False,
                "error": "home_feedback_rejected",
                "home_feedback_rejected": True,
                "checks_ok": 0,
                "height": 0,
                "decoded_height": 0,
                "width": 0,
                "decoded_width": 0,
                "download_mbps": 0.0,
                "stream_mbps": 0.0,
                "geo_restricted": False,
                "header_required": False,
                "duplicate_core_content": False,
                "short_lived_token": False,
            }
        return original_probe_channel(channel)

    def actual_height(channel) -> int:
        if is_rejected(channel):
            return 0
        return original_actual_height(channel)

    def is_stable(channel) -> bool:
        if is_rejected(channel):
            return False
        return original_is_stable(channel)

    def is_core_acceptable(channel) -> bool:
        if is_rejected(channel):
            return False
        return original_is_core_acceptable(channel)

    def measured_score(channel) -> float:
        if is_rejected(channel):
            return -1_000_000.0
        return original_measured_score(channel)

    bp.probe_channel = probe_channel
    bp.actual_height = actual_height
    bp.is_stable = is_stable
    bp.is_core_acceptable = is_core_acceptable
    bp.measured_score = measured_score
    bp.HOME_FEEDBACK_REJECTED_URLS = frozenset(bad_urls)
