#!/usr/bin/env python3
"""Build IPTV candidates in isolation without touching production playlists."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "candidate"
PRODUCTION_FILES = ("tv-easy.m3u", "tv.m3u", "tv-all.m3u", "tv-core.m3u")
CACHE_HISTORY = Path(os.environ.get("HEALTH_HISTORY_CACHE", "/tmp/iptv-health/health-history.json"))
VOLATILE_QUERY_KEYS = {
    "token", "access_token", "sign", "signature", "expires", "expire", "expiry", "exp",
    "wssecret", "wstime", "timestamp", "auth", "authkey", "auth_key", "txsecret", "txtime",
}
CCTV_RE = re.compile(r"^CCTV-(?:[1-9]|1[0-7])$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def production_hashes() -> dict[str, str]:
    return {name: sha256(ROOT / name) for name in PRODUCTION_FILES}


def normalize_route(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low in VOLATILE_QUERY_KEYS or low.startswith("token_") or low.endswith("_token"):
            continue
        query.append((key, value))
    query.sort(key=lambda item: (item[0].lower(), item[1]))
    path = re.sub(r"/+", "/", urllib.parse.unquote(parsed.path or "/"))
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, urllib.parse.urlencode(query), ""))


def parse_m3u(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    pending: tuple[str, str] | None = None
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            group_match = re.search(r'group-title="([^"]*)"', line, re.I)
            pending = (name, group_match.group(1).strip() if group_match else "")
            continue
        if pending and line.startswith(("http://", "https://")):
            entries.append({"name": pending[0], "group": pending[1], "url": line})
            pending = None
        elif line and not line.startswith("#"):
            pending = None
    return entries


def core_key(name: str, group: str, mainland_satellites: set[str]) -> str | None:
    clean = re.sub(r"\s+\[[^]]+\]\s*$", "", name).strip()
    if CCTV_RE.fullmatch(clean):
        return clean.lower().replace("-", "")
    if clean == "CCTV-5+":
        return "cctv5plus"
    if clean == "CCTV-4K":
        return "cctv4k"
    if clean in mainland_satellites:
        return clean
    return None


def core_routes(path: Path, mainland_satellites: set[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for entry in parse_m3u(path):
        key = core_key(entry["name"], entry["group"], mainland_satellites)
        if key and key not in result:
            result[key] = {
                "name": entry["name"],
                "url": entry["url"],
                "normalized_url": normalize_route(entry["url"]),
            }
    return result


def write_core_playlist(source: Path, target: Path, mainland_satellites: set[str]) -> None:
    lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
    output = [line for line in lines if line.startswith("#EXTM3U") or line.startswith("# generated_utc=")]
    pending: str | None = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXTINF"):
            pending = raw
            continue
        if pending and line.startswith(("http://", "https://")):
            name = pending.rsplit(",", 1)[-1].strip() if "," in pending else ""
            group_match = re.search(r'group-title="([^"]*)"', pending, re.I)
            group = group_match.group(1).strip() if group_match else ""
            if core_key(name, group, mainland_satellites):
                output.extend((pending, raw))
            pending = None
        elif line and not line.startswith("#"):
            pending = None
    target.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")


def load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def expected_core_keys(mainland_satellites: Iterable[str]) -> set[str]:
    return {*(f"cctv{i}" for i in range(1, 18)), "cctv5plus", "cctv4k", *mainland_satellites}


def run_builder(work: Path) -> tuple[int, str, set[str]]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_playlist as bp  # noqa: PLC0415

    mainland_satellites = set(bp.MAINLAND_SATELLITE_NAMES)
    # Do not alter CCTV/satellite identity tables: they are what makes the
    # decoded 1080/2160 core gates apply. Candidate mode only allows the build
    # to finish with missing core entries so the report can say what is absent.
    bp.MIN_EASY = 0
    previous_mode = os.environ.get("CANDIDATE_ALLOW_INCOMPLETE_CORE")
    os.environ["CANDIDATE_ALLOW_INCOMPLETE_CORE"] = "1"

    original = Path.cwd()
    try:
        os.chdir(work)
        try:
            rc = int(bp.main() or 0)
        except SystemExit as exc:
            rc = int(exc.code or 0)
        report = (work / "build-report.txt").read_text(encoding="utf-8", errors="ignore") if (work / "build-report.txt").exists() else ""
        return rc, report, mainland_satellites
    finally:
        os.chdir(original)
        if previous_mode is None:
            os.environ.pop("CANDIDATE_ALLOW_INCOMPLETE_CORE", None)
        else:
            os.environ["CANDIDATE_ALLOW_INCOMPLETE_CORE"] = previous_mode


def main() -> int:
    before = production_hashes()
    previous_manifest = load_json(CANDIDATE / "manifest.json")
    CANDIDATE.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="iptv-candidate-") as tmp:
        work = Path(tmp)
        for name in PRODUCTION_FILES:
            shutil.copy2(ROOT / name, work / name)
        if CACHE_HISTORY.exists():
            shutil.copy2(CACHE_HISTORY, work / "health-history.json")
        elif (ROOT / "health-history.json").exists():
            shutil.copy2(ROOT / "health-history.json", work / "health-history.json")

        rc, report, mainland_satellites = run_builder(work)
        build_outputs = all((work / name).exists() for name in ("tv-easy.m3u", "tv.m3u", "tv-all.m3u"))
        build_ok = rc == 0 and build_outputs

        if (work / "health-history.json").exists():
            CACHE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work / "health-history.json", CACHE_HISTORY)

        if build_ok:
            for name in ("tv-easy.m3u", "tv.m3u", "tv-all.m3u"):
                shutil.copy2(work / name, CANDIDATE / name)
            write_core_playlist(CANDIDATE / "tv-easy.m3u", CANDIDATE / "tv-core.m3u", mainland_satellites)
        else:
            for name in PRODUCTION_FILES:
                (CANDIDATE / name).unlink(missing_ok=True)

        (CANDIDATE / "build-report.txt").write_text(report, encoding="utf-8")

    after = production_hashes()
    if before != after:
        raise SystemExit("production SHA256 changed during isolated candidate build")

    production_core = core_routes(ROOT / "tv-easy.m3u", mainland_satellites)
    candidate_core = core_routes(CANDIDATE / "tv-easy.m3u", mainland_satellites) if build_ok else {}
    previous_routes = previous_manifest.get("core_routes", {}) if isinstance(previous_manifest.get("core_routes"), dict) else {}
    previous_streaks = previous_manifest.get("core_streaks", {}) if isinstance(previous_manifest.get("core_streaks"), dict) else {}

    streaks: dict[str, int] = {}
    for key, route in candidate_core.items():
        old = previous_routes.get(key, {}) if isinstance(previous_routes.get(key), dict) else {}
        same = bool(old) and old.get("normalized_url") == route.get("normalized_url")
        streaks[key] = int(previous_streaks.get(key, 0)) + 1 if same else 1

    required = expected_core_keys(mainland_satellites)
    missing_core = sorted(required - set(candidate_core))
    changed_core = sorted(
        key for key, route in candidate_core.items()
        if production_core.get(key, {}).get("normalized_url") != route.get("normalized_url")
    )
    two_round_ready = bool(build_ok) and all(streaks.get(key, 0) >= 2 for key in changed_core)
    visual_review_required = bool(changed_core)

    manifest = {
        "version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_commit": os.environ.get("GITHUB_SHA", ""),
        "build_ok": build_ok,
        "builder_return_code": rc,
        "production_sha256_before": before,
        "production_sha256_after": after,
        "production_hashes_unchanged": before == after,
        "core_routes": candidate_core,
        "production_core_routes": production_core,
        "core_streaks": streaks,
        "changed_core": changed_core,
        "missing_core": missing_core,
        "two_round_ready": two_round_ready,
        "visual_review_required": visual_review_required,
        "visual_review_confirmed": False,
        "promotion_ready": bool(build_ok and not missing_core and two_round_ready and not visual_review_required),
        "notes": "Mainland GitHub throughput is advisory; core promotion requires decoded 1080/2160 and manual visual review for changes.",
    }
    (CANDIDATE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "build_ok": build_ok,
        "changed_core": changed_core,
        "missing_core": missing_core,
        "two_round_ready": two_round_ready,
        "production_hashes_unchanged": before == after,
    }, ensure_ascii=False, indent=2))
    return 0 if build_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
