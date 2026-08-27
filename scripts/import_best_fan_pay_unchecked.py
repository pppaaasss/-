#!/usr/bin/env python3
"""Mirror unchecked best-fan pay entries into the broad fallback list only.

Safety policy: this importer never probes or decodes media, therefore its output
is NEVER eligible for tv-easy.m3u, tv-core.m3u, or tv.m3u. Unchecked entries may
only be retained in tv-all.m3u as clearly non-verified fallback inventory.
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

UPSTREAMS = (
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay.m3u8",
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay_status.m3u8",
)
PLAYLISTS = (Path("tv-all.m3u"),)
FORBIDDEN_OUTPUTS = {"tv-easy.m3u", "tv-core.m3u", "tv.m3u"}
DEFAULT_UA = "Mozilla/5.0 (AppleTV; unchecked pay fallback/3.0)"
MARKER = "# best-fan pay fallback (UNCHECKED; tv-all only)"
OLD_MARKERS = {
    "# best-fan cn_pay.m3u8 unchecked mirror (no stream probing)",
    "# best-fan pay mirror (unchecked; no stream probing)",
    MARKER,
}


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def name_key(name: str) -> str:
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", "", name)
    value = re.sub(r"\s+", "", value).lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value)


def mark_unchecked(extinf: str) -> str:
    name = visible_name(extinf)
    display = name if "[未实测]" in name else f"{name} [未实测]"
    if re.search(r'group-title="[^"]*"', extinf, re.I):
        extinf = re.sub(r'group-title="[^"]*"', 'group-title="中文付费"', extinf, flags=re.I)
    elif extinf.startswith("#EXTINF"):
        head, sep, tail = extinf.partition(",")
        extinf = f'{head} group-title="中文付费"{sep}{tail}'
    if "," in extinf:
        extinf = extinf.rsplit(",", 1)[0] + "," + display
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
            key = name_key(visible_name(extinf))
            if key:
                entries.append((key, mark_unchecked(extinf), line))
            extinf = None
        elif line and not line.startswith("#"):
            extinf = None
    return entries


def fetch_upstream() -> list[tuple[str, str, str]]:
    merged: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for upstream in UPSTREAMS:
        try:
            req = urllib.request.Request(
                upstream,
                headers={"User-Agent": DEFAULT_UA, "Accept": "application/vnd.apple.mpegurl,*/*"},
            )
            with urllib.request.urlopen(req, timeout=25) as response:
                parsed = parse_index(response.read(1_500_000).decode("utf-8", "ignore"))
            if len(parsed) < 5:
                raise RuntimeError(f"incomplete index: {len(parsed)}")
            for item in parsed:
                if item[0] not in seen:
                    seen.add(item[0])
                    merged.append(item)
        except Exception as exc:
            print(f"unchecked pay index skipped: {upstream}: {type(exc).__name__}: {str(exc)[:100]}")
    return merged


def parse_blocks(text: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    prefix: list[str] = []
    blocks: list[tuple[str, str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and i + 1 < len(lines) and lines[i + 1].strip().startswith(("http://", "https://")):
            blocks.append((name_key(visible_name(line)), line, lines[i + 1].strip()))
            i += 2
            continue
        if line.strip() not in OLD_MARKERS:
            prefix.append(line)
        i += 1
    return prefix, blocks


def patch_playlist(path: Path, upstream: list[tuple[str, str, str]]) -> int:
    if path.name in FORBIDDEN_OUTPUTS:
        raise RuntimeError(f"unchecked importer forbidden from writing {path.name}")
    if not path.exists() or not upstream:
        return 0
    original = path.read_text(encoding="utf-8", errors="ignore")
    prefix, blocks = parse_blocks(original)
    managed = {key for key, _, _ in upstream}
    kept = [(key, extinf, url) for key, extinf, url in blocks if key not in managed]
    output = list(prefix)
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
        print(f"{path}: retained {patch_playlist(path, upstream)} unchecked pay fallbacks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
