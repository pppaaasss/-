#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
DATA_DIR="${IPTV_DATA_DIR:-/var/lib/iptv-hk-probe}"
LOG_PREFIX="[HK-IPTV]"
PLAYLISTS=(tv-easy.m3u tv.m3u tv-all.m3u tv-core.m3u)

cd "$REPO_DIR"
mkdir -p "$DATA_DIR/formal" "$DATA_DIR/candidates"

# Never start from stale local production edits.
git fetch --quiet origin master
git reset --hard origin/master >/dev/null

before="$(sha256sum "${PLAYLISTS[@]}")"

python3 scripts/hk_probe.py \
  --playlist tv-core.m3u \
  --output-dir "$DATA_DIR/formal"

python3 scripts/hk_filter_harvest.py \
  --harvest harvest/candidates.jsonl \
  --playlist tv-core.m3u \
  --feedback config/home-route-feedback.json \
  --output-dir "$DATA_DIR/candidates" \
  --workers "${IPTV_HK_WORKERS:-12}" \
  --max-per-channel "${IPTV_HK_MAX_PER_CHANNEL:-14}"

python3 scripts/hk_auto_update.py \
  --formal-report "$DATA_DIR/formal/latest.json" \
  --candidate-report "$DATA_DIR/candidates/latest.json" \
  --config config/hk-auto-update.json \
  --repo-root "$REPO_DIR" \
  --summary "$DATA_DIR/auto-update-summary.json"

changed="$(git diff --name-only -- "${PLAYLISTS[@]}" || true)"
if [[ -z "$changed" ]]; then
  echo "$LOG_PREFIX no formal route updates"
  exit 0
fi

# Hard safety: the updater may only touch the four production playlists.
other="$(git diff --name-only | grep -Ev '^(tv-easy\.m3u|tv\.m3u|tv-all\.m3u|tv-core\.m3u)$' || true)"
if [[ -n "$other" ]]; then
  echo "$LOG_PREFIX refusing unexpected modified files:" >&2
  echo "$other" >&2
  git reset --hard origin/master >/dev/null
  exit 20
fi

# Hard identity/quality invariants that do not depend on network geography.
python3 - <<'PY'
from pathlib import Path
import re
files=['tv-easy.m3u','tv.m3u','tv-all.m3u','tv-core.m3u']
for fn in files:
    text=Path(fn).read_text(encoding='utf-8')
    if re.search(r',CCTV-8\s*\n[^\n]*cctv8k', text, re.I):
        raise SystemExit(f'{fn}: CCTV-8 points to cctv8k')
    if ',CCTV-8K\n' in text:
        raise SystemExit(f'{fn}: unexpected CCTV-8K identity')
print('hard identity gates passed')
PY

# Concurrent master changes must win; never overwrite newer manual work.
git fetch --quiet origin master
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/master)" ]]; then
  echo "$LOG_PREFIX master moved during probe; discard local replacements and retry next cycle"
  git reset --hard origin/master >/dev/null
  exit 0
fi

git add "${PLAYLISTS[@]}"
if git diff --cached --quiet; then
  exit 0
fi

git config user.name "hk-iptv-probe"
git config user.email "hk-iptv-probe@users.noreply.github.com"
git commit -m "Auto-replace failed IPTV routes verified from Hong Kong"

if git push origin HEAD:master; then
  echo "$LOG_PREFIX automatic production update pushed"
else
  echo "$LOG_PREFIX push failed; reverting local commit so next cycle starts clean" >&2
  git reset --hard origin/master >/dev/null
  exit 21
fi
