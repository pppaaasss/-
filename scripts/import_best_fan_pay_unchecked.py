#!/usr/bin/env python3
"""Mirror pay channels into APTV without opening stream URLs.

The upstream project already refreshes and orders its pay lists. This script
only downloads the M3U indexes, keeps the first route for each visible channel
name, forces them into 中文付费, and publishes them. It intentionally does NOT
probe, time, ffprobe, or fetch any media stream.

Both cn_pay.m3u8 and cn_pay_status.m3u8 are accepted as index sources. If one
index is temporarily unavailable the other can still refresh the mirror. A
small manual correction table is applied last for viewer-confirmed wrong-channel
mappings; those corrections remain effective even if both pay indexes fail.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

UPSTREAMS = (
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay.m3u8",
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay_status.m3u8",
)
PLAYLISTS = (Path("tv-easy.m3u"), Path("tv.m3u"), Path("tv-all.m3u"))
DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV unchecked pay mirror/2.1)"
MARKER = "# best-fan pay mirror (unchecked; no stream probing)"
OLD_MARKERS = {
    "# best-fan cn_pay.m3u8 unchecked mirror (no stream probing)",
    MARKER,
}

# Viewer reported the previous "CCTV央视台球" URL was the wrong programme.
# Keep this specialty/pay tile out of the broad CCTV/satellite bucket and use a
# route explicitly published as 央视台球 by current IPTV indexes.
MANUAL_PAY_OVERRIDES = (
    (
        "CCTV央视台球",
        '#EXTINF:-1 tvg-id="央视台球" tvg-name="央视台球" '
        'tvg-logo="https://live.fanmingming.cn/tv/%E5%A4%AE%E8%A7%86%E5%8F%B0%E7%90%83.png" '
        'group-title="中文付费",CCTV央视台球',
        "http://38.75.136.137:98/gslb/dsdqpub/ystq.m3u8?auth=testpub",
    ),
)


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def name_key(name: str) -> str:
    value = re.sub(r"\([^)]*\)", "", name)
    value = re.sub(r"\[[^]]*\]", "", value)
    value = re.sub(r"\s+", "", value).lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value)


def force_pay_group(extinf: str) -> str:
    if re.search(r'group-title="[^"]*"', extinf, re.I):
        return re.sub(r'group-title="[^"]*"', 'group-title="中文付费"', extinf, flags=re.I)
    if extinf.startswith("#EXTINF"):
        head, sep, tail = extinf.partition(",")
        return f'{head} group-title="中文付费"{sep}{tail}'
    return extinf


def parse_index(text: str) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    extinf: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            extinf = line
            continue
        if extinf and line.startswith(("http://", "https://")):
            name = visible_name(extinf)
            key = name_key(name)
            if key:
                entries.append((key, force_pay_group(extinf), line))
            extinf = None
        elif line and not line.startswith("#"):
            extinf = None
    return entries


def apply_manual_overrides(entries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    overrides = {
        name_key(name): (name_key(name), force_pay_group(extinf), url)
        for name, extinf, url in MANUAL_PAY_OVERRIDES
    }
    output = [entry for entry in entries if entry[0] not in overrides]
    output.extend(overrides.values())
    return output


def fetch_upstream() -> list[tuple[str, str, str]]:
    merged: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    failures: list[str] = []

    for upstream in UPSTREAMS:
        try:
            request = urllib.request.Request(
                upstream,
                headers={
                    "User-Agent": DEFAULT_UA,
                    "Accept": "application/vnd.apple.mpegurl,*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                text = response.read(1_500_000).decode("utf-8", "ignore")
            parsed = parse_index(text)
            if len(parsed) < 5:
                raise RuntimeError(f"index looked incomplete: {len(parsed)} entries")
            for key, extinf, url in parsed:
                if key in seen:
                    continue
                seen.add(key)
                merged.append((key, extinf, url))
            print(f"pay index loaded: {upstream} entries={len(parsed)} unique_total={len(merged)}")
        except Exception as exc:
            failures.append(f"{upstream}: {type(exc).__name__}: {str(exc)[:100]}")

    if len(merged) < 5:
        # Preserve all existing unmatched pay entries, but still let known
        # viewer corrections replace a bad tile. patch_playlist only manages
        # keys returned here, so this fallback cannot wipe the pay catalogue.
        detail = "; ".join(failures) if failures else "no usable entries"
        print(f"pay indexes unavailable; applying manual corrections only: {detail}")
        return apply_manual_overrides([])
    return apply_manual_overrides(merged)


def parse_blocks(text: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    prefix: list[str] = []
    blocks: list[tuple[str, str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and i + 1 < len(lines) and lines[i + 1].strip().startswith(("http://", "https://")):
            name = visible_name(line)
            blocks.append((name_key(name), line, lines[i + 1].strip()))
            i += 2
            continue
        if line.strip() not in OLD_MARKERS:
            prefix.append(line)
        i += 1
    return prefix, blocks


def patch_playlist(path: Path, upstream: list[tuple[str, str, str]]) -> int:
    if not path.exists():
        return 0
    original = path.read_text(encoding="utf-8", errors="ignore")
    prefix, blocks = parse_blocks(original)
    managed = {key for key, _, _ in upstream}

    # Matching visible names are owned by the pay mirror. Remove any stale
    # copy from another group, then append exactly one current upstream route.
    kept = [(key, extinf, url) for key, extinf, url in blocks if key not in managed]

    output: list[str] = []
    output.extend(prefix)
    if output and output[-1] != "":
        output.append("")
    output.append(MARKER)
    for _, extinf, url in kept:
        output.extend((extinf, url))
    for _, extinf, url in upstream:
        output.extend((extinf, url))

    rendered = "\n".join(output).rstrip() + "\n"
    if rendered != original:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return len(upstream)


def main() -> int:
    upstream = fetch_upstream()
    for path in PLAYLISTS:
        count = patch_playlist(path, upstream)
        print(f"{path}: mirrored/overrode {count} unchecked pay channels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
