#!/usr/bin/env python3
"""Build a 400-600 channel Chinese-first production lineup from HK-verified GOOD URLs.

Policy:
- preserve the existing tv-core.m3u channels exactly and place them first
- prefer Chinese mainland/local, HK/MO/TW, Chinese pay channels
- explicitly keep sports (including useful English-language sports)
- explicitly allow BBC services
- do NOT pad with generic English FAST/local/shopping channels
- one best verified route per normalized channel identity
- publish only when the resulting lineup is between min/max bounds

This script is intended to run only after the full Hong Kong URL audit completes.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from channel_regions import MAINLAND_REGION_GROUPS, regionalized_group

HAN_RE = re.compile(r"[\u3400-\u9fff]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
BBC_RE = re.compile(r"(?<![a-z])bbc(?:\s|[-_]|$)", re.I)
SPORT_RE = re.compile(
    r"(?:体育|體育|足球|篮球|籃球|网球|網球|高尔夫|高爾夫|赛车|賽車|搏击|搏擊|"
    r"sport(?:s)?|espn|bein|eurosport|dazn|nba(?:\s*tv)?|nfl|mlb|nhl|ufc|"
    r"formula\s*1|\bf1\b|motorsport|racing|golf|tennis|football|soccer|"
    r"sky\s*sports|premier\s*sports|astro\s*supersport|纬来体育|緯來體育|博斯|五星体育)",
    re.I,
)
JUNK_RE = re.compile(
    r"(?:adult|xxx|porn|情色|成人|购物|購物|shopping|test\b|测试|測試|demo\b|"
    r"radio\b|广播|廣播|电台|電台|weather\b|webcam|camera\b|traffic\b)",
    re.I,
)
QUALITY_RE = re.compile(
    r"(?:\b(?:uhd|fhd|full\s*hd|hd|sd|hevc|h265|h264|avc)\b|"
    r"(?:2160|1080|720|576|540|480)[pi]?|超高清|高清|标清|標清|蓝光|藍光|"
    r"\b(?:25|50|60)fps\b)",
    re.I,
)

GROUP_QUOTAS = {
    **{g: 5 for g in MAINLAND_REGION_GROUPS},
    "其他地方": 25,
    "中文付费": 35,
    "香港": 25,
    "澳门": 5,
    "台湾": 45,
    "新加坡": 5,
    "马来西亚": 5,
    "日本": 10,
    "体育": 35,
    "国际精选": 8,
    "娱乐": 12,
    "少儿": 10,
    "音乐": 8,
    "教育": 7,
    "财经": 10,
}

GROUP_ORDER = [
    *MAINLAND_REGION_GROUPS,
    "其他地方", "中文付费", "香港", "澳门", "台湾",
    "新加坡", "马来西亚", "体育", "国际精选", "日本",
    "娱乐", "少儿", "音乐", "教育", "财经",
]


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def playlist_blocks(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks = []
    for i, line in enumerate(lines[:-1]):
        if not line.startswith("#EXTINF:") or "," not in line:
            continue
        name = line.rsplit(",", 1)[-1].strip()
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
            j += 1
        if j >= len(lines):
            continue
        url = lines[j].strip()
        if not url.startswith(("http://", "https://")):
            continue
        gm = re.search(r'group-title="([^"]+)"', line)
        blocks.append({"name": name, "extinf": line, "url": url, "group": gm.group(1) if gm else ""})
    return blocks


def cctv_identity(raw: str) -> str | None:
    low = raw.casefold().replace("＋", "+")
    if re.search(r"cctv\s*[-_ ]?8\s*k\b", low):
        return "cctv8k"
    if re.search(r"cctv\s*[-_ ]?4\s*k\b", low):
        return "cctv4k"
    if re.search(r"cctv\s*[-_ ]?5\s*(?:\+|plus)", low):
        return "cctv5+"
    m = re.search(r"cctv\s*[-_ ]?(\d{1,2})(?!\s*[kK])", low)
    if m:
        return f"cctv{int(m.group(1))}"
    return None


def canonical(raw: str) -> str:
    c = cctv_identity(raw)
    if c:
        return c
    s = raw.casefold().replace("＋", "+")
    s = QUALITY_RE.sub("", s)
    s = re.sub(r"[\s\-_.·•()（）\[\]【】/\\]+", "", s)
    return s[:160]


def choose_name(row: dict) -> str:
    names = [str(x).strip() for x in (row.get("names") or []) if str(x).strip()]
    if not names:
        return ""
    def key(name: str):
        return (
            1 if HAN_RE.search(name) else 0,
            1 if SPORT_RE.search(name) else 0,
            1 if BBC_RE.search(name) else 0,
            -len(name),
        )
    return max(names, key=key)


def classify(name: str, groups: list[str]) -> str | None:
    text = " ".join([name, *groups])
    if not name or JUNK_RE.search(text):
        return None
    if SPORT_RE.search(text) or any(g == "体育" for g in groups):
        return "体育"
    if BBC_RE.search(name):
        return "国际精选"

    # Generic English channels are intentionally excluded. Japanese kana is a
    # separate permitted region; English sports/BBC were handled above.
    has_han = bool(HAN_RE.search(name))
    has_kana = bool(KANA_RE.search(name))
    if not has_han and not has_kana:
        return None

    preferred = next((g for g in groups if g), "中文综合")
    mapped = regionalized_group(name, preferred)
    if mapped in MAINLAND_REGION_GROUPS:
        return mapped
    if mapped in {"香港", "澳门", "台湾", "新加坡", "马来西亚", "日本"}:
        return mapped
    if preferred in {"中文付费", "娱乐", "少儿", "音乐", "教育", "财经"}:
        return preferred
    if has_kana and not has_han:
        return "日本"
    return "其他地方"


def score(row: dict) -> float:
    h = int(row.get("height") or 0)
    bitrate = float(row.get("bitrate_mbps") or 0)
    dl = float(row.get("segment_mbps") or 0)
    startup = float(row.get("startup_s") or 0)
    codec = str(row.get("codec") or "").casefold()
    value = h * 100.0 + min(bitrate, 30.0) * 250.0 + min(dl, 150.0) * 8.0 - min(startup, 30.0) * 30.0
    if codec in {"hevc", "h265"}:
        value += 250.0
    if SPORT_RE.search(" ".join(row.get("names") or [])):
        value += 100.0
    return value


def make_extinf(name: str, group: str, row: dict, existing: dict[str, dict]) -> str:
    old = existing.get(canonical(name))
    if old:
        line = str(old["extinf"])
        line = re.sub(r'group-title="[^"]*"', f'group-title="{group}"', line)
        return line
    h = int(row.get("height") or 0)
    codec = str(row.get("codec") or "")
    return f'#EXTINF:-1 group-title="{group}" hk-height="{h}" hk-codec="{codec}",{name}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--good", default="/var/lib/iptv-hk-probe/all-url-audit/good.jsonl")
    ap.add_argument("--core", default="tv-core.m3u")
    ap.add_argument("--existing-main", default="tv.m3u")
    ap.add_argument("--output-main", default="tv.m3u")
    ap.add_argument("--output-all", default="tv-all.m3u")
    ap.add_argument("--manifest", default="harvest/curated-manifest.json")
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--min-count", type=int, default=400)
    ap.add_argument("--max-count", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    good = [r for r in read_jsonl(Path(args.good)) if str(r.get("status")) == "GOOD"]
    core = playlist_blocks(Path(args.core))
    existing_blocks = playlist_blocks(Path(args.existing_main))
    existing = {canonical(x["name"]): x for x in existing_blocks}

    selected_keys = {canonical(x["name"]) for x in core}
    best: dict[str, dict] = {}
    rejected_generic_english = 0
    rejected_junk = 0

    for row in good:
        name = choose_name(row)
        groups = [str(x).strip() for x in (row.get("groups") or []) if str(x).strip()]
        if not name:
            continue
        if JUNK_RE.search(" ".join([name, *groups])):
            rejected_junk += 1
            continue
        group = classify(name, groups)
        if not group:
            rejected_generic_english += 1
            continue
        key = canonical(name)
        if not key or key in selected_keys or key == "cctv8k":
            continue
        candidate = dict(row)
        candidate["chosen_name"] = name
        candidate["chosen_group"] = group
        candidate["rank"] = score(row)
        old = best.get(key)
        if old is None or candidate["rank"] > old["rank"]:
            best[key] = candidate

    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in best.values():
        buckets[row["chosen_group"]].append(row)
    for rows in buckets.values():
        rows.sort(key=lambda r: (r["rank"], int(r.get("height") or 0)), reverse=True)

    chosen: list[dict] = []
    for group in GROUP_ORDER:
        quota = int(GROUP_QUOTAS.get(group, 0))
        for row in buckets.get(group, [])[:quota]:
            key = canonical(row["chosen_name"])
            if key in selected_keys:
                continue
            selected_keys.add(key)
            chosen.append(row)

    # Fill toward target with remaining Chinese channels first, then remaining
    # sports/BBC. Generic English never enters the eligible set at all.
    if len(core) + len(chosen) < args.target:
        used_urls = {str(r.get("url") or "") for r in chosen}
        leftovers = [
            r for r in best.values()
            if canonical(r["chosen_name"]) not in selected_keys and str(r.get("url") or "") not in used_urls
        ]
        leftovers.sort(
            key=lambda r: (
                1 if HAN_RE.search(r["chosen_name"]) else 0,
                1 if r["chosen_group"] == "体育" else 0,
                1 if r["chosen_group"] == "国际精选" else 0,
                r["rank"],
            ),
            reverse=True,
        )
        for row in leftovers:
            if len(core) + len(chosen) >= args.target:
                break
            key = canonical(row["chosen_name"])
            if key in selected_keys:
                continue
            selected_keys.add(key)
            chosen.append(row)

    # Enforce hard maximum even if future quota edits overshoot.
    chosen = chosen[: max(0, args.max_count - len(core))]
    total = len(core) + len(chosen)
    group_counts = defaultdict(int)
    for block in core:
        group_counts[block.get("group") or "卫视台"] += 1
    for row in chosen:
        group_counts[row["chosen_group"]] += 1

    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "hong_kong_verified_chinese_first_curated_lineup",
        "target": args.target,
        "minimum": args.min_count,
        "maximum": args.max_count,
        "result_channels": total,
        "core_preserved": len(core),
        "generic_english_rejected": rejected_generic_english,
        "junk_rejected": rejected_junk,
        "group_counts": dict(sorted(group_counts.items())),
        "policy": {
            "chinese_first": True,
            "sports_allowed_including_english": True,
            "bbc_allowed": True,
            "generic_english_fast_not_used_as_padding": True,
            "one_best_route_per_channel_identity": True,
            "core_playlist_preserved": True,
        },
    }
    mp = Path(args.manifest)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        f"HK_CURATED channels={total} core={len(core)} sports={group_counts.get('体育',0)} "
        f"bbc={group_counts.get('国际精选',0)} generic_english_rejected={rejected_generic_english}"
    )

    if total < args.min_count or total > args.max_count:
        print(f"HK_CURATED_NOT_PUBLISHED count={total} required={args.min_count}..{args.max_count}")
        return 4
    if args.dry_run:
        return 0

    header = [
        '#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"',
        f'# HK verified curated lineup: Chinese-first + sports + BBC; channels={total}',
        f'# generated_utc={payload["generated_utc"]}',
        '',
    ]
    lines = list(header)
    for block in core:
        lines.extend([block["extinf"], block["url"]])
    for group in GROUP_ORDER:
        rows = [r for r in chosen if r["chosen_group"] == group]
        for row in rows:
            name = row["chosen_name"]
            lines.append(make_extinf(name, group, row, existing))
            lines.append(str(row["url"]))
    text = "\n".join(lines).rstrip() + "\n"
    Path(args.output_main).write_text(text, encoding="utf-8")
    Path(args.output_all).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
