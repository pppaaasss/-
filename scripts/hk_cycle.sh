#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
DATA_DIR="${IPTV_DATA_DIR:-/var/lib/iptv-hk-probe}"
LOG_PREFIX="[HK-IPTV]"
PLAYLISTS=(tv-easy.m3u tv.m3u tv-all.m3u tv-core.m3u)

cd "$REPO_DIR"
mkdir -p "$DATA_DIR/formal"

# Never start from stale local production edits.
git fetch --quiet origin master
git reset --hard origin/master >/dev/null

before="$(sha256sum "${PLAYLISTS[@]}")"

python3 scripts/hk_probe.py \
  --playlist tv.m3u \
  --output-dir "$DATA_DIR/formal"

after="$(sha256sum "${PLAYLISTS[@]}")"
if [[ "$before" != "$after" ]]; then
  echo "$LOG_PREFIX read-only probe modified a formal playlist; restoring locked production" >&2
  git restore --source=origin/master -- "${PLAYLISTS[@]}"
  exit 20
fi

echo "$LOG_PREFIX monitoring complete; locked production playlists unchanged"
echo "$LOG_PREFIX formal report: $DATA_DIR/formal/latest.json"

python3 scripts/publish_hk_health.py \
  --report "$DATA_DIR/formal/latest.json" \
  --repository "${IPTV_GITHUB_REPOSITORY:-pppaaasss/-}" \
  --branch "${IPTV_HEALTH_BRANCH:-health-monitor}" \
  --destination "health/latest.json" \
  --token-file "${IPTV_GITHUB_TOKEN_FILE:-/etc/iptv-hk-probe.github-token}"
