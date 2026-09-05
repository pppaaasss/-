#!/opt/bin/python3
"""Versioned data contracts for the home-first IPTV control plane.

GitHub discovers text and publishes files.  It does not qualify a route.
Only the AC86U probe running on the Apple-TV-equivalent home path may create
qualified backups or an actionable replacement decision.

This module deliberately uses only the Python standard library so the same
validator can run in GitHub Actions and from Entware on the router.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit


CANDIDATE_SCHEMA = "iptv-home-candidates/v1"
BACKUP_SCHEMA = "iptv-home-qualified-backups/v1"
REPORT_SCHEMA = "iptv-home-report/v2"
ROUTE_CONTEXT = "living-room-path-equivalent"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROBE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
CHANNEL_KEY_RE = re.compile(r"^(?:cctv(?:[1-9]|1[0-7]|4k|5plus)|[^\x00-\x1f]{2,32}卫视)$")
UTC = timezone.utc

MAX_CANDIDATES = 10_000
MAX_BACKUPS = 500
MAX_RESULTS = 500

SATELLITES = (
    "北京卫视", "东方卫视", "湖南卫视", "浙江卫视", "江苏卫视", "广东卫视", "深圳卫视",
    "安徽卫视", "山东卫视", "河南卫视", "湖北卫视", "辽宁卫视", "黑龙江卫视", "四川卫视",
    "重庆卫视", "天津卫视", "河北卫视", "江西卫视", "广西卫视", "贵州卫视", "云南卫视",
    "陕西卫视", "山西卫视", "吉林卫视", "内蒙古卫视", "新疆卫视", "西藏卫视", "青海卫视",
    "甘肃卫视", "宁夏卫视", "海南卫视", "东南卫视", "延边卫视", "海峡卫视", "兵团卫视",
    "安多卫视", "农林卫视", "三沙卫视",
)
SATELLITE_ALIASES = {
    "上海卫视": "东方卫视",
    "内蒙卫视": "内蒙古卫视",
    "旅游卫视": "海南卫视",
}


class ContractError(RuntimeError):
    """Raised when an untrusted home-first payload is unsafe or ambiguous."""


def utc_text(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else float(epoch)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def parse_utc(value: object, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def object_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def url_sha256(url: str) -> str:
    return hashlib.sha256(str(url).strip().encode("utf-8")).hexdigest()


def candidate_id(channel_key: str, url: str, request_options: str = "") -> str:
    identity = f"{channel_key}\n{str(url).strip()}\n{str(request_options).strip()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _normal_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def station_key(name: object) -> str | None:
    """Return a stable key only for CCTV and mainland provincial satellites."""
    text = _normal_text(name)
    low = text.casefold().replace("＋", "+")
    compact = re.sub(r"[\s_-]+", "", text).casefold().replace("＋", "+")
    # CCTV-8K and similar labels must never be mistaken for numbered CCTV.
    if re.search(r"cctv0?\d{1,2}k", compact) and "cctv4k" not in compact:
        return None
    if "cctv4k" in compact:
        return "cctv4k"
    if re.search(r"cctv0?5(?:\+|plus|p)(?![a-z0-9])", compact):
        return "cctv5plus"
    match = re.search(r"cctv[\s_-]*0?(\d{1,2})(?!\d)(?![\s_-]*k)", low)
    if match and 1 <= int(match.group(1)) <= 17:
        return f"cctv{int(match.group(1))}"
    for alias, canonical in SATELLITE_ALIASES.items():
        if alias in text:
            return canonical
    for canonical in SATELLITES:
        if canonical in text:
            return canonical
    return None


def canonical_name(channel_key: str) -> str:
    if channel_key == "cctv4k":
        return "CCTV-4K"
    if channel_key == "cctv5plus":
        return "CCTV-5+"
    match = re.fullmatch(r"cctv(\d{1,2})", str(channel_key))
    if match:
        return f"CCTV-{int(match.group(1))}"
    return str(channel_key)


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _list(value: object, label: str, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ContractError(f"{label} must be a list with at most {maximum} items")
    return value


def _text(value: object, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be text")
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ContractError(f"{label} is invalid")
    return text


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be boolean")
    return value


def _number(value: object, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be numeric")
    number = float(value)
    if number < minimum or number > maximum:
        raise ContractError(f"{label} is outside the safe range")
    return number


def _sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise ContractError(f"{label} must be a lowercase SHA-256")
    return text


def _url(value: object, label: str) -> str:
    text = _text(value, label, 4096)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContractError(f"{label} must be an HTTP(S) URL")
    return text


def _channel_key(value: object, label: str) -> str:
    text = _text(value, label, 40)
    if not CHANNEL_KEY_RE.fullmatch(text):
        raise ContractError(f"{label} is not a supported CCTV/satellite key")
    return text


def _freshness(value: object, label: str, *, now_epoch: float | None, max_age_hours: float | None) -> datetime:
    generated = parse_utc(value, label)
    if now_epoch is None and max_age_hours is None:
        return generated
    now = datetime.fromtimestamp(time.time() if now_epoch is None else float(now_epoch), tz=UTC)
    age_hours = (now - generated).total_seconds() / 3600
    if age_hours < -(10 / 60):
        raise ContractError(f"{label} is future-dated")
    if max_age_hours is not None and age_hours > float(max_age_hours):
        raise ContractError(f"{label} is stale")
    return generated


def _playlist_binding(value: object, label: str) -> dict:
    binding = _object(value, label)
    _url(binding.get("url"), f"{label}.url")
    _sha(binding.get("sha256"), f"{label}.sha256")
    _number(binding.get("channel_count"), f"{label}.channel_count", 1, 1000)
    return binding


def make_candidate(row: dict) -> dict:
    """Normalize one discovery row without granting it any health status."""
    name = _text(row.get("name"), "candidate.name", 128)
    key = station_key(name)
    if key is None:
        raise ContractError("candidate is outside the CCTV/provincial-satellite scope")
    url = _url(row.get("url"), "candidate.url")
    options = _text(row.get("request_options", row.get("options", "")), "candidate.request_options", 2048, allow_empty=True)
    raw_sources = _list(row.get("sources"), "candidate.sources", 64)
    sources = sorted({_text(item, "candidate.sources[]", 4096) for item in raw_sources})
    if not sources:
        raise ContractError("candidate.sources must not be empty")
    return {
        "candidate_id": candidate_id(key, url, options),
        "channel_key": key,
        "name": canonical_name(key),
        "group": "卫视台",
        "url": url,
        "url_sha256": url_sha256(url),
        "request_options": options,
        "sources": sources,
        "cloud_stream_probe_performed": False,
        "home_verified": False,
        "production_eligible": False,
    }


def validate_candidate_manifest(
    payload: object,
    *,
    now_epoch: float | None = None,
    max_age_hours: float | None = None,
) -> dict:
    manifest = _object(payload, "candidate manifest")
    if manifest.get("schema") != CANDIDATE_SCHEMA:
        raise ContractError("unsupported candidate manifest schema")
    _freshness(manifest.get("generated_utc"), "candidate manifest.generated_utc", now_epoch=now_epoch, max_age_hours=max_age_hours)
    _text(manifest.get("source_revision"), "candidate manifest.source_revision", 128)
    _playlist_binding(manifest.get("formal_playlist"), "candidate manifest.formal_playlist")
    if manifest.get("scope") != ["cctv", "provincial_satellite"]:
        raise ContractError("candidate manifest scope is not home-core-only")
    if manifest.get("cloud_stream_probe_performed") is not False:
        raise ContractError("GitHub candidate manifest must not claim stream verification")
    if manifest.get("home_verified") is not False or manifest.get("production_eligible") is not False:
        raise ContractError("unverified GitHub candidates cannot be production eligible")
    candidates = _list(manifest.get("candidates"), "candidate manifest.candidates", MAX_CANDIDATES)
    if manifest.get("candidate_count") != len(candidates):
        raise ContractError("candidate manifest candidate_count is inconsistent")
    ids: set[str] = set()
    for index, value in enumerate(candidates):
        row = _object(value, f"candidate manifest.candidates[{index}]")
        key = _channel_key(row.get("channel_key"), f"candidate[{index}].channel_key")
        name = _text(row.get("name"), f"candidate[{index}].name", 128)
        if station_key(name) != key or canonical_name(key) != name:
            raise ContractError(f"candidate[{index}] name and channel key disagree")
        if row.get("group") != "卫视台":
            raise ContractError(f"candidate[{index}] group is invalid")
        url = _url(row.get("url"), f"candidate[{index}].url")
        if _sha(row.get("url_sha256"), f"candidate[{index}].url_sha256") != url_sha256(url):
            raise ContractError(f"candidate[{index}] URL hash is invalid")
        options = _text(row.get("request_options"), f"candidate[{index}].request_options", 2048, allow_empty=True)
        identity = _sha(row.get("candidate_id"), f"candidate[{index}].candidate_id")
        if identity != candidate_id(key, url, options) or identity in ids:
            raise ContractError(f"candidate[{index}] identity is invalid or duplicated")
        ids.add(identity)
        sources = _list(row.get("sources"), f"candidate[{index}].sources", 64)
        if not sources:
            raise ContractError(f"candidate[{index}] has no source references")
        for source in sources:
            _text(source, f"candidate[{index}].sources[]", 4096)
        for field in ("cloud_stream_probe_performed", "home_verified", "production_eligible"):
            if row.get(field) is not False:
                raise ContractError(f"candidate[{index}] illegally claims {field}")
    expected_set = object_sha256(candidates)
    if _sha(manifest.get("candidate_set_sha256"), "candidate manifest.candidate_set_sha256") != expected_set:
        raise ContractError("candidate manifest candidate set hash is invalid")
    return manifest


def _verification(value: object, label: str, *, require_deep: bool) -> dict:
    check = _object(value, label)
    sample_count = int(_number(check.get("sample_count"), f"{label}.sample_count", 0, 4))
    _number(check.get("startup_s"), f"{label}.startup_s", 0, 600)
    _number(check.get("min_download_mbps"), f"{label}.min_download_mbps", 0, 20_000)
    _number(check.get("stream_mbps"), f"{label}.stream_mbps", 0, 500)
    _number(check.get("headroom_ratio"), f"{label}.headroom_ratio", 0, 100_000)
    _number(check.get("width"), f"{label}.width", 0, 16_384)
    _number(check.get("height"), f"{label}.height", 0, 8_640)
    _number(check.get("fps"), f"{label}.fps", 0, 240)
    _number(check.get("bitrate_mbps"), f"{label}.bitrate_mbps", 0, 500)
    _text(check.get("codec"), f"{label}.codec", 32, allow_empty=True)
    deep = _boolean(check.get("deep_checked"), f"{label}.deep_checked")
    if require_deep and (not deep or sample_count < 2 or int(check.get("height") or 0) <= 0):
        raise ContractError(f"{label} lacks deep two-sample evidence")
    return check


def validate_backup_pool(
    payload: object,
    *,
    expected_probe_id: str | None = None,
    now_epoch: float | None = None,
    allow_expired: bool = False,
) -> dict:
    pool = _object(payload, "backup pool")
    if pool.get("schema") != BACKUP_SCHEMA:
        raise ContractError("unsupported backup pool schema")
    probe_id = _text(pool.get("probe_id"), "backup pool.probe_id", 48)
    if not PROBE_ID_RE.fullmatch(probe_id) or (expected_probe_id and probe_id != expected_probe_id):
        raise ContractError("backup pool probe_id is invalid")
    generated = _freshness(pool.get("generated_utc"), "backup pool.generated_utc", now_epoch=now_epoch, max_age_hours=None)
    if pool.get("route_context") != ROUTE_CONTEXT:
        raise ContractError("backup pool was not measured on the living-room path")
    _sha(pool.get("formal_playlist_sha256"), "backup pool.formal_playlist_sha256")
    _sha(pool.get("candidate_manifest_sha256"), "backup pool.candidate_manifest_sha256")
    backups = _list(pool.get("backups"), "backup pool.backups", MAX_BACKUPS)
    if pool.get("backup_count") != len(backups):
        raise ContractError("backup pool backup_count is inconsistent")
    ids: set[str] = set()
    for index, value in enumerate(backups):
        row = _object(value, f"backup pool.backups[{index}]")
        key = _channel_key(row.get("channel_key"), f"backup[{index}].channel_key")
        name = _text(row.get("name"), f"backup[{index}].name", 128)
        if station_key(name) != key or canonical_name(key) != name:
            raise ContractError(f"backup[{index}] name and key disagree")
        url = _url(row.get("url"), f"backup[{index}].url")
        if _sha(row.get("url_sha256"), f"backup[{index}].url_sha256") != url_sha256(url):
            raise ContractError(f"backup[{index}] URL hash is invalid")
        options = _text(row.get("request_options"), f"backup[{index}].request_options", 2048, allow_empty=True)
        identity = _sha(row.get("candidate_id"), f"backup[{index}].candidate_id")
        if identity != candidate_id(key, url, options) or identity in ids:
            raise ContractError(f"backup[{index}] identity is invalid or duplicated")
        ids.add(identity)
        if row.get("qualification") != "QUALIFIED":
            raise ContractError(f"backup[{index}] is not qualified")
        _sha(row.get("source_manifest_sha256"), f"backup[{index}].source_manifest_sha256")
        qualified = parse_utc(row.get("qualified_utc"), f"backup[{index}].qualified_utc")
        verified = parse_utc(row.get("last_verified_utc"), f"backup[{index}].last_verified_utc")
        expires = parse_utc(row.get("expires_utc"), f"backup[{index}].expires_utc")
        if not qualified <= verified <= expires or verified > generated:
            raise ContractError(f"backup[{index}] timestamps are inconsistent")
        if not allow_expired and now_epoch is not None and expires.timestamp() < float(now_epoch):
            raise ContractError(f"backup[{index}] is expired")
        _verification(row.get("verification"), f"backup[{index}].verification", require_deep=True)
    return pool


def _current_result(value: object, label: str) -> tuple[str, str, str, bool]:
    row = _object(value, label)
    key = _channel_key(row.get("channel_key"), f"{label}.channel_key")
    name = _text(row.get("name"), f"{label}.name", 128)
    if station_key(name) != key or canonical_name(key) != name:
        raise ContractError(f"{label} name and key disagree")
    url = _url(row.get("url"), f"{label}.url")
    if _sha(row.get("url_sha256"), f"{label}.url_sha256") != url_sha256(url):
        raise ContractError(f"{label} URL hash is invalid")
    status = str(row.get("status") or "")
    if status not in {"GOOD", "BAD", "UNKNOWN"}:
        raise ContractError(f"{label}.status is invalid")
    confirmed = _boolean(row.get("failure_confirmed"), f"{label}.failure_confirmed")
    if confirmed != (status == "BAD"):
        raise ContractError(f"{label} BAD/failure confirmation is inconsistent")
    attempts = int(_number(row.get("attempt_count"), f"{label}.attempt_count", 1, 2))
    if status == "BAD" and attempts != 2:
        raise ContractError(f"{label} BAD status lacks two attempts")
    _verification(row.get("verification"), f"{label}.verification", require_deep=False)
    return key, url, status, confirmed


def _candidate_result(value: object, label: str) -> tuple[str, str, str, bool]:
    row = _object(value, label)
    key = _channel_key(row.get("channel_key"), f"{label}.channel_key")
    url = _url(row.get("url"), f"{label}.url")
    options = _text(row.get("request_options"), f"{label}.request_options", 2048, allow_empty=True)
    identity = _sha(row.get("candidate_id"), f"{label}.candidate_id")
    if identity != candidate_id(key, url, options):
        raise ContractError(f"{label} identity is invalid")
    status = str(row.get("qualification") or "")
    if status not in {"QUALIFIED", "REJECTED", "UNKNOWN"}:
        raise ContractError(f"{label}.qualification is invalid")
    purpose = str(row.get("purpose") or "")
    if purpose not in {"daily-qualification", "switch-reverification", "primary-cache"}:
        raise ContractError(f"{label}.purpose is invalid")
    switch_reverified = _boolean(row.get("switch_reverified"), f"{label}.switch_reverified")
    if switch_reverified and (purpose != "switch-reverification" or status != "QUALIFIED"):
        raise ContractError(f"{label} has invalid switch reverification evidence")
    _verification(row.get("verification"), f"{label}.verification", require_deep=status == "QUALIFIED")
    return identity, key, status, switch_reverified


def validate_home_report_v2(
    payload: object,
    *,
    expected_probe_id: str | None = None,
    now_epoch: float | None = None,
    max_age_hours: float | None = None,
) -> dict:
    report = _object(payload, "home report")
    if report.get("schema") != REPORT_SCHEMA:
        raise ContractError("unsupported home report schema")
    probe_id = _text(report.get("probe_id"), "home report.probe_id", 48)
    if not PROBE_ID_RE.fullmatch(probe_id) or (expected_probe_id and probe_id != expected_probe_id):
        raise ContractError("home report probe_id is invalid")
    _freshness(report.get("generated_utc"), "home report.generated_utc", now_epoch=now_epoch, max_age_hours=max_age_hours)
    if report.get("run_kind") not in {"primary-0200", "recheck-1300"}:
        raise ContractError("home report run_kind is invalid")
    if report.get("run_status") != "COMPLETED" or report.get("production_modified") is not False:
        raise ContractError("router report must be complete and must not modify production")
    actionable = _boolean(report.get("actionable"), "home report.actionable")
    if report.get("route_context") != ROUTE_CONTEXT:
        raise ContractError("home report did not use the living-room-equivalent path")
    _playlist_binding(report.get("formal_playlist"), "home report.formal_playlist")
    baseline = _object(report.get("baseline"), "home report.baseline")
    baseline_safe = all(
        _boolean(baseline.get(field), f"home report.baseline.{field}")
        for field in ("home_network_ok", "github_reachable", "route_verified")
    ) and not _boolean(baseline.get("mass_failure_circuit_breaker"), "home report.baseline.mass_failure_circuit_breaker")

    current = _list(report.get("current_results"), "home report.current_results", MAX_RESULTS)
    if not current:
        raise ContractError("home report has no current results")
    current_by_key: dict[str, tuple[str, str, bool]] = {}
    for index, value in enumerate(current):
        key, url, status, confirmed = _current_result(value, f"current_results[{index}]")
        if key in current_by_key:
            raise ContractError("home report duplicates a current channel")
        current_by_key[key] = (url, status, confirmed)

    candidates = _list(report.get("candidate_results"), "home report.candidate_results", MAX_CANDIDATES)
    candidate_status: dict[str, tuple[str, str, bool]] = {}
    for index, value in enumerate(candidates):
        identity, key, status, switch_reverified = _candidate_result(value, f"candidate_results[{index}]")
        if identity in candidate_status:
            raise ContractError("home report duplicates a candidate result")
        candidate_status[identity] = (key, status, switch_reverified)
    for row in candidates:
        cached = row.get("purpose") == "primary-cache"
        if report["run_kind"] == "recheck-1300" and not cached:
            raise ContractError("13:00 report must use cached primary evidence; no backup probes")
        if cached:
            if report["run_kind"] != "recheck-1300" or row.get("verified_run_kind") != "primary-0200":
                raise ContractError("cached backup was not verified by a primary run")
            if row.get("switch_reverified") is not False or row.get("qualification") != "QUALIFIED":
                raise ContractError("cached backup must not claim a new probe")
            verified = parse_utc(row.get("last_verified_utc"), "cached backup.last_verified_utc")
            expires = parse_utc(row.get("expires_utc"), "cached backup.expires_utc")
            generated = parse_utc(report["generated_utc"], "home report.generated_utc")
            if not verified <= generated <= expires or not 0 < (expires - verified).total_seconds() <= 36 * 3600:
                raise ContractError("cached backup is expired or has invalid verification times")
            if now_epoch is not None and expires.timestamp() < now_epoch:
                raise ContractError("cached backup expired before publication")

    decisions = _list(report.get("decisions"), "home report.decisions", MAX_RESULTS)
    decision_keys: set[str] = set()
    for index, value in enumerate(decisions):
        row = _object(value, f"decisions[{index}]")
        key = _channel_key(row.get("channel_key"), f"decisions[{index}].channel_key")
        if key in decision_keys or key not in current_by_key:
            raise ContractError(f"decisions[{index}] references an invalid channel")
        decision_keys.add(key)
        action = str(row.get("action") or "")
        if action not in {"KEEP", "REPLACE", "UNRESOLVED"}:
            raise ContractError(f"decisions[{index}].action is invalid")
        _text(row.get("reason"), f"decisions[{index}].reason", 240)
        replacement = row.get("replacement_candidate_id")
        _, current_status, confirmed = current_by_key[key]
        if action == "REPLACE":
            if not actionable or not baseline_safe or current_status != "BAD" or not confirmed:
                raise ContractError("replacement lacks safe confirmed home evidence")
            identity = _sha(replacement, f"decisions[{index}].replacement_candidate_id")
            evidence = candidate_status.get(identity)
            if evidence is None or evidence[0] != key or evidence[1] != "QUALIFIED":
                raise ContractError("replacement candidate was not qualified at home")
            if report["run_kind"] == "primary-0200" and evidence[2] is not True:
                raise ContractError("replacement candidate was not reverified before switching")
        elif replacement is not None:
            raise ContractError("non-replacement decision contains a replacement candidate")
    if decision_keys != set(current_by_key):
        raise ContractError("home report decisions do not cover every current channel")
    return report
