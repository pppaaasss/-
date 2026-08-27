#!/usr/bin/env python3
"""Compatibility entry point: build isolated candidates, never production.

The former rebuild pipeline directly mutated tv-easy.m3u/tv.m3u/tv-all.m3u/
tv-core.m3u and chained several legacy repair/import passes. That publication
path is retired. Formal playlists may now change only through the manual
candidate promotion workflow (or the manual rollback/emergency-fix workflow),
all sharing the production-publish-lock.
"""
from __future__ import annotations

from build_candidate import main as build_candidate


def main() -> int:
    print("Legacy production rebuild is disabled; generating candidate/ only.", flush=True)
    return build_candidate()


if __name__ == "__main__":
    raise SystemExit(main())
