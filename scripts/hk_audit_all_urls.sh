#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
DATA_DIR="${IPTV_DATA_DIR:-/var/lib/iptv-hk-probe}"
LOCK_FILE="/run/iptv-hk-probe.lock"
WORKERS="${IPTV_HK_ALL_WORKERS:-4}"

cd "$REPO_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another Hong Kong IPTV probe/audit is already running." >&2
  exit 10
fi

# Start from the current repository source/harvest. Never touch production files.
git fetch --quiet origin master || true
git reset --hard origin/master >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/all-url-audit"

exec python3 scripts/hk_audit_all_urls.py \
  --harvest harvest/candidates.jsonl \
  --output-dir "$DATA_DIR/all-url-audit" \
  --workers "$WORKERS" \
  "$@"
