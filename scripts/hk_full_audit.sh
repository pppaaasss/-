#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
DATA_DIR="${IPTV_DATA_DIR:-/var/lib/iptv-hk-probe}/full-audit"
WORKERS="${IPTV_HK_FULL_WORKERS:-4}"

cd "$REPO_DIR"
git fetch --quiet origin master
git reset --hard origin/master >/dev/null
mkdir -p "$DATA_DIR"

exec python3 scripts/hk_probe.py \
  --playlist tv.m3u \
  --core-playlist tv-core.m3u \
  --default-min-height 720 \
  --workers "$WORKERS" \
  --output-dir "$DATA_DIR"
