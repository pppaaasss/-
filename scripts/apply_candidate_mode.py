#!/usr/bin/env python3
"""Allow incomplete candidate scans without relaxing decoded quality gates."""
from __future__ import annotations

from pathlib import Path

TARGET = Path("scripts/build_playlist.py")
MARKER = "CANDIDATE_ALLOW_INCOMPLETE_CORE"


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")
    changed = False

    guarded_cctv = f'    if easy_missing_cctv and not os.getenv("{MARKER}"):\n'
    if guarded_cctv not in source:
        old = "    if easy_missing_cctv:\n"
        if old not in source:
            raise SystemExit("candidate CCTV safety anchor not found")
        source = source.replace(old, guarded_cctv, 1)
        changed = True

    guarded_sat = f'    if easy_missing_satellites and not os.getenv("{MARKER}"):\n'
    if guarded_sat not in source:
        old = "    if easy_missing_satellites:\n"
        if old not in source:
            raise SystemExit("candidate satellite safety anchor not found")
        source = source.replace(old, guarded_sat, 1)
        changed = True

    guarded_legacy = f'    if legacy_safety_reasons and not os.getenv("{MARKER}"):\n'
    if guarded_legacy not in source:
        old = "    if legacy_safety_reasons:\n"
        if old not in source:
            raise SystemExit("candidate partial-output safety anchor not found")
        source = source.replace(old, guarded_legacy, 1)
        changed = True

    compile(source, str(TARGET), "exec")
    if changed:
        TARGET.write_text(source, encoding="utf-8", newline="\n")
        print("applied isolated candidate mode; decoded quality gates unchanged")
    else:
        print("candidate mode already fully applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
