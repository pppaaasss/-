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

python3 scripts/dead_only_failover.py \
  --formal-report "$DATA_DIR/formal/latest.json" \
  --config config/dead-only-failover.json \
  --repo-root "$REPO_DIR" \
  --summary "$DATA_DIR/dead-only-failover.json" \
  --apply

changed="$(git diff --name-only -- "${PLAYLISTS[@]}" || true)"
if [[ -z "$changed" ]]; then
  echo "$LOG_PREFIX no confirmed DEAD route with a qualified fixed spare"
  exit 0
fi

other="$(git diff --name-only | grep -Ev '^(tv-easy\.m3u|tv\.m3u|tv-all\.m3u|tv-core\.m3u)$' || true)"
if [[ -n "$other" ]]; then
  echo "$LOG_PREFIX refusing unexpected modified files: $other" >&2
  git restore --source=origin/master -- "${PLAYLISTS[@]}"
  exit 30
fi

python3 - "$DATA_DIR/dead-only-failover.json" <<'PY'
import json, sys
data=json.load(open(sys.argv[1], encoding='utf-8'))
assert data.get('applied') is True
assert data.get('selected_updates')
assert data.get('policy', {}).get('dead_only') is True
PY

git fetch --quiet origin master
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/master)" ]]; then
  echo "$LOG_PREFIX master moved during confirmation; discard and retry next cycle"
  git reset --hard origin/master >/dev/null
  exit 0
fi

git add "${PLAYLISTS[@]}"
git config user.name "hk-iptv-dead-failover"
git config user.email "hk-iptv-dead-failover@users.noreply.github.com"
git commit -m "Replace repeatedly confirmed dead IPTV routes"
if git push origin HEAD:master; then
  echo "$LOG_PREFIX confirmed DEAD route replacement pushed"
else
  echo "$LOG_PREFIX push failed; restoring remote master" >&2
  git reset --hard origin/master >/dev/null
  exit 31
fi
