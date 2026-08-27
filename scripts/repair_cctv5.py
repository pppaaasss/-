#!/usr/bin/env python3
"""Read-only CCTV-5/CCTV-5+ candidate check.

Direct post-build mutation of the four production playlists is retired. CCTV-5
and CCTV-5+ are selected by the decoded-quality builder inside candidate/ and
may reach production only through the manual reviewed promotion workflow.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_EASY = ROOT / "candidate" / "tv-easy.m3u"
TARGETS = {"CCTV-5", "CCTV-5+"}


def find_routes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    pending = ""
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            name = re.sub(r"\s+\[[^]]+\]\s*$", "", name).strip()
            pending = name if name in TARGETS else ""
        elif pending and line.startswith(("http://", "https://")):
            found[pending] = line
            pending = ""
        elif line and not line.startswith("#"):
            pending = ""
    return found


def main() -> int:
    routes = find_routes(CANDIDATE_EASY)
    missing = sorted(TARGETS - set(routes))
    for name in sorted(routes):
        print(f"candidate {name}: {routes[name]}")
    if missing:
        print("candidate missing decoded-quality route: " + ", ".join(missing))
        return 2
    print("CCTV-5 pair is candidate-only; no production playlist was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
