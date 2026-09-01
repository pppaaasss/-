#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
DATA_DIR="${IPTV_DATA_DIR:-/var/lib/iptv-hk-probe}"
LOG_PREFIX="[HK-IPTV]"
PLAYLISTS=(tv-easy.m3u tv.m3u tv-all.m3u tv-core.m3u)
TIMER_FILE="/etc/systemd/system/iptv-hk-probe.timer"
CRON_FILE="/etc/cron.d/iptv-hk-probe"

# RETIRED: Hong Kong reachability is not evidence for playback at home.  Keep
# the implementation below as rollback evidence, but make every installed copy
# self-disable before any network probe, report upload, playlist edit, or push.
echo "$LOG_PREFIX retired: AC86U home reports are the sole IPTV health authority"
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  systemctl disable --now iptv-hk-probe.timer >/dev/null 2>&1 || true
fi
if [[ -f "$CRON_FILE" ]]; then
  mv -f "$CRON_FILE" "$CRON_FILE.retired"
  if command -v rc-service >/dev/null 2>&1; then
    rc-service crond restart >/dev/null 2>&1 || true
  elif command -v service >/dev/null 2>&1; then
    service cron restart >/dev/null 2>&1 || service crond restart >/dev/null 2>&1 || true
  fi
fi
echo "$LOG_PREFIX no probe, upload, failover, or production write was attempted"
exit 0

cd "$REPO_DIR"
mkdir -p "$DATA_DIR/formal"

# Never start from stale local production edits.
git fetch --quiet origin master
git reset --hard origin/master >/dev/null

# Keep an already-installed Hong Kong host on the repository's current cadence.
# This lets an existing four-hour installation migrate itself to six hours on
# the next cycle without requiring the installer to be run again manually.
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system && -f "$TIMER_FILE" ]]; then
  if ! grep -qF 'OnCalendar=*-*-* 00/6:17:00' "$TIMER_FILE"; then
    sed -i \
      -e 's/Description=Run Hong Kong IPTV health probe every four hours/Description=Run Hong Kong IPTV health probe every six hours/' \
      -e 's#^OnCalendar=.*#OnCalendar=*-*-* 00/6:17:00#' \
      "$TIMER_FILE"
    systemctl daemon-reload
    systemctl restart iptv-hk-probe.timer
    echo "$LOG_PREFIX scheduler migrated to 00:17/06:17/12:17/18:17"
  fi
elif [[ -f "$CRON_FILE" ]]; then
  desired='17 */6 * * * root /usr/local/sbin/iptv-hk-probe >> /var/log/iptv-hk-probe.log 2>&1'
  if ! grep -qF "$desired" "$CRON_FILE"; then
    cat > "$CRON_FILE" <<'EOF'
# Hong Kong IPTV monitor every 6 hours.
# Production changes are allowed only for repeatedly confirmed DEAD routes.
17 */6 * * * root /usr/local/sbin/iptv-hk-probe >> /var/log/iptv-hk-probe.log 2>&1
EOF
    chmod 0644 "$CRON_FILE"
    if command -v rc-service >/dev/null 2>&1; then
      rc-service crond restart
    elif command -v service >/dev/null 2>&1; then
      service cron restart || service crond restart
    fi
    echo "$LOG_PREFIX cron migrated to minute 17 every six hours"
  fi
fi

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

# A home report can only appear here through the dedicated SSH forced-command
# receiver.  Publishing is optional until the router has been paired; a bad or
# absent home report must never break the independent Hong Kong safety chain.
HOME_REPORT="$DATA_DIR/home/latest.json"
if [[ -s "$HOME_REPORT" ]]; then
  if ! python3 scripts/publish_hk_health.py \
    --report "$HOME_REPORT" \
    --repository "${IPTV_GITHUB_REPOSITORY:-pppaaasss/-}" \
    --branch "${IPTV_HEALTH_BRANCH:-health-monitor}" \
    --destination "health/home-latest.json" \
    --token-file "${IPTV_GITHUB_TOKEN_FILE:-/etc/iptv-hk-probe.github-token}"
  then
    echo "$LOG_PREFIX warning: validated home report upload failed; Hong Kong evidence remains valid" >&2
  fi
else
  echo "$LOG_PREFIX home receiver not paired yet; continuing with Hong Kong evidence only"
fi

python3 scripts/dead_only_failover.py \
  --formal-report "$DATA_DIR/formal/latest.json" \
  --config config/dead-only-failover.json \
  --repo-root "$REPO_DIR" \
  --summary "$DATA_DIR/dead-only-failover.json" \
  --apply

# Return the on-demand candidate decisions to GitHub as well. This report is
# read-only unless a repeatedly confirmed DEAD route found a freshly qualified
# spare; normal and merely degraded channels never enter candidate probing.
if ! python3 scripts/publish_hk_health.py \
  --report "$DATA_DIR/dead-only-failover.json" \
  --repository "${IPTV_GITHUB_REPOSITORY:-pppaaasss/-}" \
  --branch "${IPTV_HEALTH_BRANCH:-health-monitor}" \
  --destination "health/dead-only-failover.json" \
  --token-file "${IPTV_GITHUB_TOKEN_FILE:-/etc/iptv-hk-probe.github-token}"
then
  echo "$LOG_PREFIX warning: failover diagnostics upload failed; main health report remains valid" >&2
fi

changed="$(git diff --name-only -- "${PLAYLISTS[@]}" || true)"
if [[ -z "$changed" ]]; then
  echo "$LOG_PREFIX no confirmed DEAD route with a freshly qualified spare"
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
