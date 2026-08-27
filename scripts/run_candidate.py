#!/usr/bin/env python3
"""Run the isolated candidate builder with candidate-only ranking policy."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_playlist as bp  # noqa: E402
from candidate_policy import apply  # noqa: E402
from build_candidate import main  # noqa: E402

apply(bp)

if __name__ == "__main__":
    raise SystemExit(main())
