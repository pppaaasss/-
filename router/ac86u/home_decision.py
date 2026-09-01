#!/opt/bin/python3
"""Pure home-side qualification and replacement decision helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from .home_contract import (
        BACKUP_SCHEMA,
        ROUTE_CONTEXT,
        canonical_name,
        url_sha256,
        utc_text,
        validate_backup_pool,
    )
except ImportError:  # Installed beside this file on the router.
    from home_contract import (  # type: ignore
        BACKUP_SCHEMA,
        ROUTE_CONTEXT,
        canonical_name,
        url_sha256,
        utc_text,
        validate_backup_pool,
    )


UTC = timezone.utc
MAX_BACKUPS_PER_CHANNEL = 8


def probe_verification(result: dict) -> dict:
    return {
        "sample_count": int(result.get("sample_count") or 0),
        "startup_s": float(result.get("startup_s") or 0),
        "min_download_mbps": float(result.get("min_download_mbps") or 0),
        "stream_mbps": float(result.get("stream_mbps") or 0),
        "headroom_ratio": float(result.get("headroom_ratio") or 0),
        "width": int(result.get("width") or 0),
        "height": int(result.get("height") or 0),
        "codec": str(result.get("codec") or ""),
        "fps": float(result.get("fps") or 0),
        "bitrate_mbps": float(result.get("bitrate_mbps") or 0),
        "deep_checked": bool(result.get("deep_checked")),
    }


def probe_is_good(result: dict) -> bool:
    return (
        result.get("observed_status") == "GOOD"
        and int(result.get("sample_count") or 0) == 2
    )


def candidate_is_qualified(result: dict) -> bool:
    return (
        probe_is_good(result)
        and result.get("deep_checked") is True
        and int(result.get("height") or 0) >= int(result.get("min_height") or 0)
    )


def mass_failure_circuit(
    attempts_by_key: dict[str, list[dict]],
    *,
    minimum_channels: int,
    failure_ratio: float,
) -> bool:
    """Stop all replacement decisions when one run looks globally unhealthy."""
    if not attempts_by_key:
        return True
    failed = sum(
        not any(probe_is_good(attempt) for attempt in attempts)
        for attempts in attempts_by_key.values()
    )
    return failed >= max(1, int(minimum_channels)) and failed / len(attempts_by_key) >= float(failure_ratio)


def current_result(name: str, url: str, attempts: list[dict], *, circuit_open: bool) -> dict:
    """Collapse one or two full route attempts into a fail-closed result.

    A first-pass GOOD route is kept.  A first failure followed by recovery is
    UNKNOWN (flaky but not safe to replace).  Two non-GOOD attempts become BAD
    only when the mass-failure circuit is closed.
    """
    if not attempts:
        raise RuntimeError("current route has no probe attempts")
    first = attempts[0]
    if circuit_open:
        status = "UNKNOWN"
        chosen = first
    elif probe_is_good(first):
        status = "GOOD"
        chosen = first
    elif len(attempts) >= 2 and probe_is_good(attempts[1]):
        status = "UNKNOWN"
        chosen = attempts[1]
    elif len(attempts) >= 2:
        status = "BAD"
        chosen = attempts[-1]
    else:
        status = "UNKNOWN"
        chosen = first
    return {
        "channel_key": str(chosen.get("channel_key") or ""),
        "name": name,
        "url": url,
        "url_sha256": url_sha256(url),
        "status": status,
        "failure_confirmed": status == "BAD",
        "attempt_count": len(attempts),
        "verification": probe_verification(chosen),
        "error": str(chosen.get("error") or "")[:400],
    }


def candidate_result(
    candidate: dict,
    result: dict,
    *,
    purpose: str,
    switch_reverified: bool,
) -> dict:
    if candidate_is_qualified(result):
        qualification = "QUALIFIED"
    elif result.get("observed_status") == "UNKNOWN":
        qualification = "UNKNOWN"
    else:
        qualification = "REJECTED"
    return {
        "candidate_id": str(candidate["candidate_id"]),
        "channel_key": str(candidate["channel_key"]),
        "url": str(candidate["url"]),
        "request_options": str(candidate.get("request_options") or ""),
        "qualification": qualification,
        "purpose": purpose,
        "switch_reverified": bool(switch_reverified and qualification == "QUALIFIED"),
        "verification": probe_verification(result),
        "error": str(result.get("error") or "")[:400],
    }


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def update_backup_pool(
    existing: dict | None,
    qualified: list[tuple[dict, dict]],
    *,
    probe_id: str,
    now_epoch: float,
    formal_playlist_sha256: str,
    candidate_manifest_sha256: str,
    current_urls: dict[str, str],
    ttl_hours: float,
) -> dict:
    now = datetime.fromtimestamp(float(now_epoch), tz=UTC)
    kept: dict[str, dict] = {}
    if isinstance(existing, dict):
        try:
            validate_backup_pool(existing, expected_probe_id=probe_id, now_epoch=now_epoch, allow_expired=True)
            for row in existing.get("backups") or []:
                expires = _parse_utc(str(row["expires_utc"]))
                key = str(row["channel_key"])
                if expires >= now and str(row["url"]) != current_urls.get(key):
                    kept[str(row["candidate_id"])] = dict(row)
        except Exception:
            # A corrupt local pool is discarded, never trusted for replacement.
            kept = {}

    expires_text = utc_text((now + timedelta(hours=max(1.0, float(ttl_hours)))).timestamp())
    now_text = utc_text(now_epoch)
    for candidate, result in qualified:
        if not candidate_is_qualified(result):
            continue
        key = str(candidate["channel_key"])
        if str(candidate["url"]) == current_urls.get(key):
            continue
        identity = str(candidate["candidate_id"])
        previous = kept.get(identity) or {}
        kept[identity] = {
            "candidate_id": identity,
            "channel_key": key,
            "name": canonical_name(key),
            "url": str(candidate["url"]),
            "url_sha256": url_sha256(str(candidate["url"])),
            "request_options": str(candidate.get("request_options") or ""),
            "qualification": "QUALIFIED",
            "source_manifest_sha256": str(candidate.get("source_manifest_sha256") or candidate_manifest_sha256),
            "qualified_utc": str(previous.get("qualified_utc") or now_text),
            "last_verified_utc": now_text,
            "expires_utc": expires_text,
            "verification": probe_verification(result),
        }

    ranked = sorted(
        kept.values(),
        key=lambda row: (str(row["channel_key"]), -backup_score(row), str(row["candidate_id"])),
    )
    backups: list[dict] = []
    per_channel: dict[str, int] = {}
    for row in ranked:
        key = str(row["channel_key"])
        if per_channel.get(key, 0) >= MAX_BACKUPS_PER_CHANNEL:
            continue
        backups.append(row)
        per_channel[key] = per_channel.get(key, 0) + 1
    pool = {
        "schema": BACKUP_SCHEMA,
        "probe_id": probe_id,
        "generated_utc": now_text,
        "route_context": ROUTE_CONTEXT,
        "formal_playlist_sha256": formal_playlist_sha256,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "backup_count": len(backups),
        "backups": backups,
    }
    validate_backup_pool(pool, expected_probe_id=probe_id, now_epoch=now_epoch)
    return pool


def backup_score(row: dict) -> float:
    verification = row.get("verification") or {}
    return (
        int(verification.get("height") or 0) * 1_000_000
        + min(float(verification.get("fps") or 0), 60) * 10_000
        + min(float(verification.get("stream_mbps") or verification.get("bitrate_mbps") or 0), 100) * 100
        + min(float(verification.get("headroom_ratio") or 0), 100)
    )


def eligible_backups(pool: dict | None, channel_key: str, *, now_epoch: float) -> list[dict]:
    if not isinstance(pool, dict):
        return []
    now = datetime.fromtimestamp(float(now_epoch), tz=UTC)
    rows = [
        dict(row)
        for row in pool.get("backups") or []
        if str(row.get("channel_key")) == channel_key
        and row.get("qualification") == "QUALIFIED"
        and _parse_utc(str(row.get("expires_utc"))) >= now
    ]
    return sorted(rows, key=lambda row: (-backup_score(row), str(row.get("candidate_id"))))


def backup_refresh_candidates(
    pool: dict | None,
    *,
    now_epoch: float,
    refresh_before_hours: float,
) -> list[dict]:
    """Return locally trusted backups that need a fresh 02:00 qualification."""
    if not isinstance(pool, dict):
        return []
    cutoff = datetime.fromtimestamp(float(now_epoch), tz=UTC) + timedelta(
        hours=max(0.0, float(refresh_before_hours))
    )
    rows: list[dict] = []
    for backup in pool.get("backups") or []:
        if _parse_utc(str(backup.get("expires_utc"))) > cutoff:
            continue
        rows.append({
            "candidate_id": str(backup["candidate_id"]),
            "channel_key": str(backup["channel_key"]),
            "url": str(backup["url"]),
            "request_options": str(backup.get("request_options") or ""),
            "source_manifest_sha256": str(backup["source_manifest_sha256"]),
            "_queue_priority": 0,
            "_expires_utc": str(backup["expires_utc"]),
        })
    return sorted(rows, key=lambda row: (row["_expires_utc"], row["channel_key"], row["candidate_id"]))


def without_backup(pool: dict, candidate_identity: str) -> dict:
    value = dict(pool)
    rows = [dict(row) for row in pool.get("backups") or [] if row.get("candidate_id") != candidate_identity]
    value["backups"] = rows
    value["backup_count"] = len(rows)
    return value
