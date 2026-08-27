#!/usr/bin/env python3
"""Fetch upstream IPTV text only and build a raw, deduplicated harvest pool.

This stage intentionally performs *no* stream probing. GitHub Actions is only
used as a convenient public-source fetcher; reachability, resolution, bitrate
and playback quality are delegated to the Hong Kong probe host.

Outputs under harvest/ are raw research inputs and never production playlists.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_playlist as bp  # noqa: E402

USER_AGENT = "Mozilla/5.0 (GitHub IPTV source harvester/1.0)"
FETCH_TIMEOUT = 22
READ_LIMIT = 12 * 1024 * 1024
WORKERS = 12
URL_RE = re.compile(r"https?://[^\s]+", re.I)
GROUP_RE = re.compile(r'group-title=["\']([^"\']+)["\']', re.I)


def clean_name(value: str) -> str:
    value = value.replace("\ufeff", "").strip().strip("\"'")
    value = re.sub(r"\s+", " ", value)
    return value[:180]


def split_url_options(raw: str) -> tuple[str, str]:
    raw = raw.strip().strip("\"'")
    raw = raw.rstrip(",;")
    if "$" in raw:
        url, options = raw.split("$", 1)
        return url.strip(), options.strip()
    return raw, ""


def valid_url(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


def urls_from_text(value: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in URL_RE.finditer(value):
        raw = match.group(0).strip()
        # Chinese TXT pools commonly separate alternate routes with '#'.
        raw = raw.split("#", 1)[0]
        url, options = split_url_options(raw)
        if valid_url(url):
            out.append((url, options))
    return out


def fetch_text(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain,application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = resp.read(READ_LIMIT)
        final = resp.geturl()
    return data.decode("utf-8", "ignore"), final


def parse_source(text: str, configured_group: str, source_url: str) -> list[dict]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[dict] = []
    current_txt_group = configured_group
    i = 0
    while i < len(lines):
        raw = lines[i].strip().replace("\ufeff", "")
        if not raw:
            i += 1
            continue

        if raw.startswith("#EXTINF:"):
            name = clean_name(raw.rsplit(",", 1)[-1] if "," in raw else "")
            gm = GROUP_RE.search(raw)
            group = clean_name(gm.group(1)) if gm else configured_group
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt or nxt.startswith("#"):
                    j += 1
                    continue
                found = urls_from_text(nxt)
                if found and name:
                    for url, options in found:
                        rows.append({
                            "name": name,
                            "group": group or configured_group,
                            "url": url,
                            "options": options,
                            "source": source_url,
                        })
                break
            i = max(i + 1, j)
            continue

        # Chinese TXT convention: 分组名,#genre#
        if ",#genre#" in raw.lower():
            current_txt_group = clean_name(raw.split(",", 1)[0]) or configured_group
            i += 1
            continue

        # Common TXT convention: 频道名,http://... ; retain every URL on line.
        if "," in raw and "http" in raw.lower():
            name, rest = raw.split(",", 1)
            name = clean_name(name)
            if name:
                for url, options in urls_from_text(rest):
                    rows.append({
                        "name": name,
                        "group": current_txt_group or configured_group,
                        "url": url,
                        "options": options,
                        "source": source_url,
                    })
        i += 1
    return rows


def load_feedback_bad_urls(path: Path) -> set[str]:
    # Harvest deliberately keeps bad routes as research evidence. This count is
    # metadata only; the Hong Kong filter applies the actual hard veto later.
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    bad = data.get("bad") if isinstance(data, dict) else None
    if not isinstance(bad, dict):
        return set()
    return {str(url).strip() for url in bad if str(url).strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="harvest")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--feedback", default="config/home-route-feedback.json")
    args = ap.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    source_defs = list(bp.SOURCES)
    failures: list[dict] = []
    source_stats: list[dict] = []

    def one(item: tuple[str, str, bool]) -> tuple[list[dict], dict]:
        group, url, _flag = item
        started = time.monotonic()
        try:
            text, final = fetch_text(url)
            rows = parse_source(text, group, url)
            return rows, {
                "group": group,
                "source": url,
                "final_url": final,
                "ok": True,
                "entries": len(rows),
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        except Exception as exc:
            return [], {
                "group": group,
                "source": url,
                "ok": False,
                "entries": 0,
                "elapsed_s": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}:{str(exc)[:240]}",
            }

    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for got, stat in ex.map(one, source_defs):
            rows.extend(got)
            source_stats.append(stat)
            if not stat.get("ok"):
                failures.append(stat)

    # Curated direct routes are raw inputs too. They are not probed here.
    for extra in getattr(bp, "EXTRAS", []):
        if not isinstance(extra, (tuple, list)) or len(extra) < 3:
            continue
        name, group, url = str(extra[0]), str(extra[1]), str(extra[2])
        clean_url, options = split_url_options(url)
        if clean_name(name) and valid_url(clean_url):
            rows.append({
                "name": clean_name(name),
                "group": clean_name(group),
                "url": clean_url,
                "options": options,
                "source": "curated:EXTRAS",
            })

    # Dedupe by channel label + exact URL. Preserve all upstream provenance.
    merged: dict[tuple[str, str], dict] = {}
    provenance: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        name = clean_name(str(row.get("name") or ""))
        url = str(row.get("url") or "").strip()
        if not name or not valid_url(url):
            continue
        key = (name, url)
        provenance[key].add(str(row.get("source") or ""))
        if key not in merged:
            merged[key] = {
                "name": name,
                "group": clean_name(str(row.get("group") or "")),
                "url": url,
                "options": str(row.get("options") or ""),
            }

    candidates = []
    for key, row in merged.items():
        row = dict(row)
        row["sources"] = sorted(x for x in provenance[key] if x)
        candidates.append(row)
    candidates.sort(key=lambda x: (x["group"], x["name"].casefold(), x["url"]))

    jsonl = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in candidates)
    (out / "candidates.jsonl").write_text(jsonl, encoding="utf-8")

    bad_urls = load_feedback_bad_urls(Path(args.feedback))
    unique_urls = {row["url"] for row in candidates}
    manifest = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "github_source_harvest_only",
        "stream_probe_performed": False,
        "production_modified": False,
        "source_definitions": len(source_defs),
        "sources_fetched_ok": sum(bool(x.get("ok")) for x in source_stats),
        "sources_failed": len(failures),
        "raw_entries_before_dedupe": len(rows),
        "candidate_entries": len(candidates),
        "unique_urls": len(unique_urls),
        "known_home_bad_urls_present_for_research": len(unique_urls & bad_urls),
        "sha256_candidates_jsonl": hashlib.sha256(jsonl.encode("utf-8")).hexdigest(),
        "policy": {
            "github_only_fetches_text": True,
            "github_does_not_judge_stream_reachability": True,
            "hong_kong_host_is_filter_stage": True,
            "home_feedback_is_final_veto": True,
        },
        "sources": sorted(source_stats, key=lambda x: (str(x.get("group")), str(x.get("source")))),
        "failures": failures,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        f"HARVEST sources={manifest['source_definitions']} ok={manifest['sources_fetched_ok']} "
        f"failed={manifest['sources_failed']} entries={manifest['candidate_entries']} "
        f"unique_urls={manifest['unique_urls']}"
    )
    # A few upstream failures are normal. Fail only if the harvest is clearly empty.
    return 0 if len(candidates) >= 100 else 2


if __name__ == "__main__":
    raise SystemExit(main())
