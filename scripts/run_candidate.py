#!/usr/bin/env python3
"""Run the isolated candidate builder with candidate-only safety policies."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_playlist as bp  # noqa: E402
from candidate_policy import apply  # noqa: E402
from gap_candidate_patch import apply as apply_gap_patch  # noqa: E402
from home_route_policy import apply as apply_home_route_policy  # noqa: E402
from build_candidate import main  # noqa: E402

apply(bp)
apply_gap_patch(bp)
# Apply home feedback last so it is the final veto over every ranking patch.
apply_home_route_policy(bp)

if __name__ == "__main__":
    raise SystemExit(main())
