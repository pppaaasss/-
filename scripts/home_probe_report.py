#!/usr/bin/env python3
"""Validation helpers for reports produced by the home IPTV probe.

The report crosses a trust boundary: an inexpensive router submits it to the
Hong Kong host, and a later GitHub job may use it as production evidence.  Keep
the schema deliberately small and reject ambiguity instead of normalising it.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


SCHEMA = "iptv-home-probe/v1"
MAX_REPORT_BYTES = 900_000
PROBE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATUSES = {"GOOD", "DEGRADED", "UNKNOWN", "DEAD"}
MODES = {"light", "deep"}
UTC = timezone.utc


def parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError("home report is missing generated_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("home report has an invalid generated_utc") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("home report generated_utc must include a timezone")
    return parsed.astimezone(UTC)


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"home report {label} must be an object")
    return value


def _list(value, label: str) -> list:
    if not isinstance(value, list):
        raise RuntimeError(f"home report {label} must be a list")
    return value


def _boolean(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"home report {label} must be boolean")
    return value


def _number(value, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"home report {label} must be numeric")
    number = float(value)
    if number < minimum or number > maximum:
        raise RuntimeError(f"home report {label} is outside the safe range")
    return number


def _url(value, label: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"home report {label} must be an HTTP(S) URL")
    if len(text) > 4096:
        raise RuntimeError(f"home report {label} is too long")
    return text


def _sha(value, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise RuntimeError(f"home report {label} must be a lowercase SHA-256")
    return text


def _validate_playlist(value, label: str, *, require_count: bool = True) -> dict:
    playlist = _object(value, label)
    _url(playlist.get("url"), f"{label}.url")
    _sha(playlist.get("sha256"), f"{label}.sha256")
    if require_count:
        _number(playlist.get("channel_count"), f"{label}.channel_count", 1, 1000)
    return playlist


def _validate_sample(value, label: str) -> None:
    sample = _object(value, label)
    _url(sample.get("url"), f"{label}.url")
    _number(sample.get("downloaded_bytes"), f"{label}.downloaded_bytes", 0, 16 * 1024 * 1024)
    _number(sample.get("total_bytes"), f"{label}.total_bytes", 0, 256 * 1024 * 1024)
    _number(sample.get("duration_s"), f"{label}.duration_s", 0, 180)
    _number(sample.get("elapsed_s"), f"{label}.elapsed_s", 0, 180)
    _number(sample.get("download_mbps"), f"{label}.download_mbps", 0, 20_000)
    _number(sample.get("stream_mbps"), f"{label}.stream_mbps", 0, 500)
    _boolean(sample.get("complete"), f"{label}.complete")


def _validate_result(value, label: str, *, candidate: bool = False) -> tuple[str, str]:
    row = _object(value, label)
    name = str(row.get("name") or "").strip()
    if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
        raise RuntimeError(f"home report {label}.name is invalid")
    url = _url(row.get("url"), f"{label}.url")
    if _sha(row.get("url_sha256"), f"{label}.url_sha256") != hashlib.sha256(url.encode()).hexdigest():
        raise RuntimeError(f"home report {label}.url_sha256 does not match its URL")
    status = str(row.get("status") or "")
    observed = str(row.get("observed_status") or "")
    if status not in STATUSES or observed not in {"GOOD", "DEGRADED", "UNKNOWN"}:
        raise RuntimeError(f"home report {label} has an invalid status")
    _number(row.get("sample_count"), f"{label}.sample_count", 0, 4)
    samples = _list(row.get("segment_samples"), f"{label}.segment_samples")
    if len(samples) > 4 or int(row.get("sample_count")) != len(samples):
        raise RuntimeError(f"home report {label} has inconsistent segment samples")
    for index, sample in enumerate(samples):
        _validate_sample(sample, f"{label}.segment_samples[{index}]")
    for field, maximum in (
        ("startup_s", 600),
        ("min_download_mbps", 20_000),
        ("avg_download_mbps", 20_000),
        ("stream_mbps", 500),
        ("headroom_ratio", 100_000),
        ("fps", 240),
        ("bitrate_mbps", 500),
        ("failure_age_hours", 100_000),
        ("degraded_age_hours", 100_000),
    ):
        _number(row.get(field), f"{label}.{field}", 0, maximum)
    _number(row.get("width"), f"{label}.width", 0, 16384)
    _number(row.get("height"), f"{label}.height", 0, 8640)
    _number(row.get("min_height"), f"{label}.min_height", 1, 8640)
    _number(row.get("consecutive_failures"), f"{label}.consecutive_failures", 0, 100_000)
    _number(row.get("consecutive_degraded"), f"{label}.consecutive_degraded", 0, 100_000)
    _boolean(row.get("home_dead_confirmed"), f"{label}.home_dead_confirmed")
    _boolean(row.get("home_degraded_confirmed"), f"{label}.home_degraded_confirmed")
    if status == "DEAD" and not row.get("home_dead_confirmed"):
        raise RuntimeError(f"home report {label} marks DEAD without confirmation")
    if candidate:
        _boolean(row.get("candidate_confirmed"), f"{label}.candidate_confirmed")
        if row.get("candidate_confirmed") and status != "GOOD":
            raise RuntimeError(f"home report {label} confirms a non-GOOD candidate")
    error = str(row.get("error") or "")
    if len(error) > 600:
        raise RuntimeError(f"home report {label}.error is too long")
    return name, url


def validate_home_report(
    report: dict,
    *,
    expected_probe_id: str | None = None,
    now_epoch: float | None = None,
    max_age_hours: float | None = None,
    require_receiver: bool = False,
) -> dict:
    """Validate a decoded report and return it unchanged."""
    report = _object(report, "root")
    if report.get("schema") != SCHEMA:
        raise RuntimeError("unsupported home report schema")
    probe_id = str(report.get("probe_id") or "")
    if not PROBE_ID_RE.fullmatch(probe_id):
        raise RuntimeError("home report probe_id is invalid")
    if expected_probe_id is not None and probe_id != expected_probe_id:
        raise RuntimeError("home report probe_id does not match this receiver")
    if report.get("run_status") != "COMPLETED":
        raise RuntimeError("home report is not a completed run")
    if report.get("mode") not in MODES:
        raise RuntimeError("home report mode is invalid")
    _boolean(report.get("actionable"), "actionable")
    if report.get("production_modified") is not False:
        raise RuntimeError("home report does not prove production_modified=false")

    generated = parse_utc(str(report.get("generated_utc") or ""))
    if now_epoch is not None or max_age_hours is not None:
        now = datetime.fromtimestamp(time.time() if now_epoch is None else now_epoch, tz=UTC)
        age = (now - generated).total_seconds() / 3600
        if age < -(10 / 60):
            raise RuntimeError("home report is future-dated")
        if max_age_hours is not None and age > float(max_age_hours):
            raise RuntimeError("home report is stale")

    playlist = _validate_playlist(report.get("playlist"), "playlist")
    policy = _object(report.get("policy"), "policy")
    if policy.get("auto_replace_formal_routes") is not False:
        raise RuntimeError("home probe itself must not replace production routes")
    _boolean(policy.get("mass_failure_circuit_breaker"), "policy.mass_failure_circuit_breaker")
    _number(policy.get("sample_bytes"), "policy.sample_bytes", 512 * 1024, 12 * 1024 * 1024)
    _number(policy.get("samples_per_route"), "policy.samples_per_route", 2, 4)

    resources = _object(report.get("resources"), "resources")
    _number(resources.get("load1"), "resources.load1", 0, 10_000)
    _number(resources.get("mem_available_kib"), "resources.mem_available_kib", 0, 1024 * 1024 * 1024)
    _number(resources.get("runtime_s"), "resources.runtime_s", 0, 24 * 3600)

    rows = _list(report.get("results"), "results")
    if not rows or len(rows) > 1000:
        raise RuntimeError("home report results count is invalid")
    names: set[str] = set()
    statuses = {status: 0 for status in STATUSES}
    for index, value in enumerate(rows):
        name, _ = _validate_result(value, f"results[{index}]")
        if name in names:
            raise RuntimeError("home report contains duplicate channel results")
        names.add(name)
        statuses[str(value.get("status"))] += 1
    if int(playlist.get("channel_count")) != len(rows):
        raise RuntimeError("home report playlist channel count does not match results")

    candidates = _list(report.get("candidate_results"), "candidate_results")
    if len(candidates) > len(rows):
        raise RuntimeError("home report has too many candidate results")
    candidate_names: set[str] = set()
    for index, value in enumerate(candidates):
        name, _ = _validate_result(value, f"candidate_results[{index}]", candidate=True)
        if name not in names or name in candidate_names:
            raise RuntimeError("home report candidate channel is missing or duplicated")
        candidate_names.add(name)
    candidate_playlist = report.get("candidate_playlist")
    if candidates:
        _validate_playlist(candidate_playlist, "candidate_playlist")
    elif candidate_playlist is not None:
        _validate_playlist(candidate_playlist, "candidate_playlist")

    summary = _object(report.get("summary"), "summary")
    _boolean(summary.get("circuit_breaker_open"), "summary.circuit_breaker_open")
    expected = {
        "channels": len(rows),
        "good": statuses["GOOD"],
        "degraded": statuses["DEGRADED"],
        "unknown": statuses["UNKNOWN"],
        "dead": statuses["DEAD"],
        "candidate_channels": len(candidates),
        "candidate_confirmed": sum(bool(row.get("candidate_confirmed")) for row in candidates),
    }
    for field, value in expected.items():
        if summary.get(field) != value:
            raise RuntimeError(f"home report summary.{field} is inconsistent")

    if require_receiver:
        transport = _object(report.get("transport"), "transport")
        if transport.get("receiver_validated") is not True or transport.get("via") != "ssh-forced-command":
            raise RuntimeError("home report lacks receiver validation")
        parse_utc(str(transport.get("received_utc") or ""))
        _sha(transport.get("report_sha256"), "transport.report_sha256")
    return report


def load_home_report_bytes(
    raw: bytes,
    *,
    expected_probe_id: str | None = None,
    now_epoch: float | None = None,
    max_age_hours: float | None = None,
    require_receiver: bool = False,
) -> dict:
    if len(raw) > MAX_REPORT_BYTES:
        raise RuntimeError("home report is too large")
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("home report is not valid UTF-8 JSON") from exc
    return validate_home_report(
        report,
        expected_probe_id=expected_probe_id,
        now_epoch=now_epoch,
        max_age_hours=max_age_hours,
        require_receiver=require_receiver,
    )


def report_fingerprint(report: dict) -> str:
    """Fingerprint router-authored content, excluding receiver annotations."""
    clean = dict(report)
    clean.pop("transport", None)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
