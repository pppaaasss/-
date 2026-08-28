#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
DATA_DIR="${IPTV_DATA_DIR:-/var/lib/iptv-hk-probe}"
AUDIT_DIR="$DATA_DIR/all-url-audit"
SNAPSHOT_DIR="$DATA_DIR/verified-pool-snapshot"
TOKEN_FILE="/etc/iptv-hk-probe.github-token"
ASKPASS="/usr/local/libexec/iptv-hk-git-askpass"
LOG="${IPTV_FINALIZE_LOG:-/var/log/iptv-hk-finalize.log}"

mkdir -p "$SNAPSHOT_DIR"

echo "[HK-FINALIZE] waiting for full audit completion" | tee -a "$LOG"
while true; do
  if [[ -s "$AUDIT_DIR/summary.json" ]]; then
    state="$(python3 - "$AUDIT_DIR/summary.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
t=int(p.get('total_unique_urls') or 0); c=int(p.get('completed') or 0)
print(f'{c}:{t}')
PY
)"
    done_count="${state%%:*}"
    total_count="${state##*:}"
    if [[ "$total_count" -gt 0 && "$done_count" -eq "$total_count" ]]; then
      break
    fi
  fi
  sleep 60
done

echo "[HK-FINALIZE] audit complete; building GOOD-only active pool" | tee -a "$LOG"
cd "$REPO_DIR"
git fetch --quiet origin master
git reset --hard origin/master >/dev/null

python3 scripts/hk_finalize_verified_pool.py \
  --audit-dir "$AUDIT_DIR" \
  --repo-root "$REPO_DIR" | tee -a "$LOG"

cp harvest/candidates.jsonl "$SNAPSHOT_DIR/candidates.jsonl"
cp harvest/pending.jsonl "$SNAPSHOT_DIR/pending.jsonl"
cp harvest/rejected-url-sha256.txt "$SNAPSHOT_DIR/rejected-url-sha256.txt"
cp harvest/verified-manifest.json "$SNAPSHOT_DIR/verified-manifest.json"

# Production playlists are sacred during pool cleanup.
test -z "$(git status --porcelain -- tv-easy.m3u tv.m3u tv-all.m3u tv-core.m3u)"

git add \
  harvest/candidates.jsonl \
  harvest/pending.jsonl \
  harvest/rejected-url-sha256.txt \
  harvest/verified-manifest.json

if git diff --cached --quiet; then
  echo "[HK-FINALIZE] no repository pool changes" | tee -a "$LOG"
  exit 0
fi

git config user.name "hk-iptv-probe"
git config user.email "hk-iptv-probe@users.noreply.github.com"
git commit -m "Replace raw IPTV inventory with Hong Kong verified pool" >/dev/null

if [[ -s "$TOKEN_FILE" ]]; then
  export GIT_TERMINAL_PROMPT=0
  export GIT_ASKPASS="$ASKPASS"
  if git push origin HEAD:master; then
    echo "[HK-FINALIZE] verified pool pushed to GitHub" | tee -a "$LOG"
    rm -f "$SNAPSHOT_DIR/NEEDS_PUBLISH"
    exit 0
  fi
fi

# Keep an external snapshot even if GitHub credentials are not configured yet.
touch "$SNAPSHOT_DIR/NEEDS_PUBLISH"
echo "[HK-FINALIZE] GitHub write credential unavailable/failed; verified snapshot saved at $SNAPSHOT_DIR" | tee -a "$LOG"
exit 0
