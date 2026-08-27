#!/usr/bin/env python3
"""Add an incomplete-candidate escape hatch without relaxing quality gates."""
from __future__ import annotations

from pathlib import Path

TARGET = Path("scripts/build_playlist.py")
MARKER = "CANDIDATE_ALLOW_INCOMPLETE_CORE"


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")
    if f'os.getenv("{MARKER}")' in source:
        print("candidate incomplete-core mode already applied")
        return 0
    old_cctv = "    if easy_missing_cctv:\n"
    old_sat = "    if easy_missing_satellites:\n"
    if old_cctv not in source or old_sat not in source:
        raise SystemExit("candidate-mode safety anchors not found")
    source = source.replace(
        old_cctv,
        f'    if easy_missing_cctv and not os.getenv("{MARKER}"):\n',
        1,
    )
    source = source.replace(
        old_sat,
        f'    if easy_missing_satellites and not os.getenv("{MARKER}"):\n',
        1,
    )
    compile(source, str(TARGET), "exec")
    TARGET.write_text(source, encoding="utf-8", newline="\n")
    print("applied candidate incomplete-core mode; quality gates unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
