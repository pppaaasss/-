#!/usr/bin/env python3
"""Filter GitHub-harvested IPTV links from a Hong Kong host.

GitHub harvests text; this program is the network/quality judge. It focuses on
formal core channel names, excludes current formal URLs and home-rejected URLs,
probes real decoded video + HLS segments, and writes a *candidate* report only.
It never edits tv*.m3u production playlists.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import hk_probe  # noqa: E402

QUALITY_WORDS_RE = re.compile(
    r"(?:超高清|高清|标清|藍光|蓝光|频道|頻道|综合|綜合|财经|財經|综艺|綜藝|体育|體育|"
    r"电影|電影|电视剧|電視劇|纪录|紀錄|科教|戏曲|戲曲|社会与法|新聞|新闻|少儿|少兒|音乐|音樂|农业农村|"
    r"uhd|fhd|fullhd|hd|sd|hevc|h265|h264|avc|1080[pi]?|2160p?|4k|50fps|25fps)",
    re.I,
)
TOKEN_HINT_RE = re.compile(r"(?:^|[?&])(token|auth_key|expires?|expire|sign|wssecret|wstime)=", re.I)
CCTV_RE = re.compile(r"cctv\s*[-_ ]?(\d{1,2})(?!\s*[kK])", re.I)


def compact(value: str) -> str:
    value = value.casefold().replace("＋", "+")
    value = QUALITY_WORDS_RE.sub("", value)
    return re.sub(r"[\s\-_.·•()（）\[\]【】]+", "", value)


def cctv_key(raw: str) -> str | None:
    low = raw.casefold().replace("＋", "+")
    if re.search(r"cctv\s*[-_ ]?8\s*k\b", low):
        return "CCTV-8K"
    if re.search(r"cctv\s*[-_ ]?4\s*k\b", low):
        return "CCTV-4K"
    if re.search(r"cctv\s*[-_ ]?5\s*(?:\+|plus)", low):
        return "CCTV-5+"
    m = CCTV_RE.search(low)
    if m:
        return f"CCTV-{int(m.group(1))}"
    return None


def build_target_matcher(targets: list[str]):
    exact = {compact(x): x for x in targets}
    non_cctv = sorted((x for x in targets if not x.startswith("CCTV-")), key=len, reverse=True)

    def match(raw: str) -> str | None:
        ck = cctv_key(raw)
        if ck:
            return ck if ck in targets else None
        c = compact(raw)
        if c in exact:
            return exact[c]
        for target in non_cctv:
            tc = compact(target)
            if tc and tc in c:
                return target
        return None

    return match


def load_harvest(path: Path) -> list[dict]:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if isinstance(row, dict) and str(row.get("url") or "").startswith(("http://", "https://")):
            rows.append(row)
    return rows


def load_bad_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    bad = data.get("bad") if isinstance(data, dict) else None
    if isinstance(bad, dict):
        return {str(x).strip() for x in bad if str(x).strip()}
    return set()


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").casefold()
    except Exception:
        return ""


def diverse_take(rows: list[dict], limit: int) -> list[dict]:
    """Prefer host diversity before taking a second route from the same host."""
    by_host: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_host[host_of(str(row["url"]))].append(row)
    hosts = sorted(by_host, key=lambda h: (-len(by_host[h]), h))
    out: list[dict] = []
    depth = 0
    while len(out) < limit:
        added = False
        for host in hosts:
            bucket = by_host[host]
            if depth < len(bucket):
                out.append(bucket[depth])
                added = True
                if len(out) >= limit:
                    break
        if not added:
            break
        depth += 1
    return out


def score_result(row: dict) -> float:
    r = row["probe"]
    if r["status"] != "GOOD":
        return -1e9
    score = 100000.0
    score += min(float(r.get("height") or 0), 2160) * 8
    bitrate = float(r.get("bitrate_mbps") or 0)
    if bitrate:
        score += min(bitrate, 20.0) * 180
    score += min(float(r.get("segment_mbps") or 0), 100.0) * 12
    score -= min(float(r.get("startup_s") or 0), 30.0) * 25
    if row.get("token_hint"):
        score -= 800
    if row.get("low_bitrate_warning"):
        score -= 1200
    return round(score, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", default="harvest/candidates.jsonl")
    ap.add_argument("--playlist", default="tv-core.m3u")
    ap.add_argument("--feedback", default="config/home-route-feedback.json")
    ap.add_argument("--output-dir", default="/var/lib/iptv-hk-probe/candidates")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--max-per-channel", type=int, default=14)
    ap.add_argument("--list-only", action="store_true", help="Parse/match only; perform no stream probes")
    args = ap.parse_args()

    harvest_path = Path(args.harvest)
    if not harvest_path.exists():
        print(f"harvest missing: {harvest_path}")
        return 3

    formal = hk_probe.load_playlist(Path(args.playlist))
    formal_urls = {name: url for name, url in formal}
    targets = [name for name, _ in formal]
    matcher = build_target_matcher(targets)
    bad_urls = load_bad_urls(Path(args.feedback))

    buckets: dict[str, list[dict]] = defaultdict(list)
    rejected = defaultdict(int)
    seen: set[tuple[str, str]] = set()
    for raw in load_harvest(harvest_path):
        target = matcher(str(raw.get("name") or ""))
        if not target:
            continue
        url = str(raw.get("url") or "").strip()
        if target == "CCTV-8" and "cctv8k" in url.casefold():
            rejected["cctv8k_identity"] += 1
            continue
        if url in bad_urls:
            rejected["home_feedback"] += 1
            continue
        if url == formal_urls.get(target):
            rejected["same_as_formal"] += 1
            continue
        key = (target, url)
        if key in seen:
            continue
        seen.add(key)
        row = dict(raw)
        row["target"] = target
        row["host"] = host_of(url)
        row["token_hint"] = bool(TOKEN_HINT_RE.search(url))
        buckets[target].append(row)

    selected: list[dict] = []
    matched_counts = {}
    for target in targets:
        rows = buckets.get(target, [])
        matched_counts[target] = len(rows)
        selected.extend(diverse_take(rows, max(1, args.max_per_channel)))

    if args.list_only:
        print(
            f"HK_FILTER_LIST_ONLY targets={len(targets)} harvested_matches={sum(matched_counts.values())} "
            f"selected={len(selected)} home_rejected={rejected['home_feedback']}"
        )
        if "CCTV-8" in matched_counts:
            print(f"CCTV-8 candidates={matched_counts['CCTV-8']} cctv8k_rejected={rejected['cctv8k_identity']}")
        return 0 if selected else 4

    def do_probe(row: dict) -> dict:
        result = hk_probe.probe_one((str(row["target"]), str(row["url"])))
        p = asdict(result)
        # Known very-low H264 intrinsic bitrate is hard evidence of bad 1080.
        bitrate = float(p.get("bitrate_mbps") or 0)
        low_warn = bool(p.get("status") == "GOOD" and p.get("codec") == "h264" and 0 < bitrate < 2.0)
        if p.get("status") == "GOOD" and p.get("codec") == "h264" and 0 < bitrate < 1.0:
            p["status"] = "DEGRADED"
            p["error"] = f"h264_intrinsic_bitrate_{bitrate:.3f}Mbps_too_low"
        out = dict(row)
        out["probe"] = p
        out["low_bitrate_warning"] = low_warn
        out["score"] = 0.0
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        probed = list(ex.map(do_probe, selected))
    for row in probed:
        row["score"] = score_result(row)

    by_target: dict[str, list[dict]] = defaultdict(list)
    for row in probed:
        by_target[str(row["target"])].append(row)
    for rows in by_target.values():
        rows.sort(key=lambda x: (x["score"], x["probe"].get("height", 0)), reverse=True)

    winners = {}
    for target in targets:
        good = [x for x in by_target.get(target, []) if x["probe"]["status"] == "GOOD"]
        if good:
            winners[target] = good[0]

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe_region": "Hong Kong",
        "stage": "hong_kong_filter_of_github_harvest",
        "production_modified": False,
        "formal_playlist": str(args.playlist),
        "harvest": str(args.harvest),
        "policy": {
            "github_does_not_probe_streams": True,
            "home_feedback_hard_veto": True,
            "cctv8k_never_matches_cctv8": True,
            "decoded_height_floor_1080": True,
            "cctv4k_height_floor_2160": True,
            "hk_network_failure_is_unknown": True,
            "auto_replace_formal_routes": False,
        },
        "summary": {
            "formal_targets": len(targets),
            "harvest_matches": sum(matched_counts.values()),
            "selected_for_probe": len(selected),
            "good": sum(x["probe"]["status"] == "GOOD" for x in probed),
            "degraded": sum(x["probe"]["status"] == "DEGRADED" for x in probed),
            "unknown": sum(x["probe"]["status"] == "UNKNOWN" for x in probed),
            "channels_with_good_spare": len(winners),
        },
        "rejected_before_probe": dict(rejected),
        "matched_counts": matched_counts,
        "winners": winners,
        "results": probed,
    }
    (out / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    m3u = ["#EXTM3U", "# Hong Kong filtered spare routes; NOT production"]
    for target in targets:
        row = winners.get(target)
        if not row:
            continue
        p = row["probe"]
        m3u.append(
            f'#EXTINF:-1 group-title="HK筛选备胎" hk-height="{p.get("height",0)}" hk-codec="{p.get("codec","")}" hk-score="{row["score"]}",{target}'
        )
        m3u.append(str(row["url"]))
    (out / "latest.m3u").write_text("\n".join(m3u) + "\n", encoding="utf-8")

    text = [
        f"generated_utc={payload['generated_utc']}",
        f"targets={len(targets)} selected={len(selected)} GOOD={payload['summary']['good']} "
        f"DEGRADED={payload['summary']['degraded']} UNKNOWN={payload['summary']['unknown']} "
        f"good_spares={len(winners)}",
    ]
    for target in targets:
        row = winners.get(target)
        if row:
            p = row["probe"]
            text.append(
                f"SPARE {target:12} {p.get('width',0)}x{p.get('height',0)} {p.get('codec',''):5} "
                f"bitrate={float(p.get('bitrate_mbps') or 0):.2f}M dl={float(p.get('segment_mbps') or 0):.2f}M "
                f"host={row.get('host','')}"
            )
        else:
            text.append(f"NONE  {target:12} matched={matched_counts.get(target,0)}")
    (out / "latest.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    print("\n".join(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
