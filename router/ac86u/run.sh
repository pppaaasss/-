#!/bin/sh
set -eu

BASE="/opt/share/iptv-home-probe"
CONFIG="/opt/etc/iptv-home-probe.json"
DATA="/opt/var/lib/iptv-home-probe"
LOG="/opt/var/log/iptv-home-probe.log"
LOCK="/opt/var/run/iptv-home-probe.lock"

mkdir -p "$DATA" "$(dirname "$LOG")" "$(dirname "$LOCK")"
if ! mkdir "$LOCK" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT HUP INT TERM

if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ]; then
  mv -f "$LOG" "$LOG.1"
fi

mode="auto"
if [ "${1:-}" = "--mode" ] && [ -n "${2:-}" ]; then
  mode="$2"
fi

run_probe() {
  if command -v ionice >/dev/null 2>&1; then
    exec ionice -c 3 nice -n 15 /opt/bin/python3 "$BASE/home_probe.py" --config "$CONFIG" --mode "$mode"
  fi
  exec nice -n 15 /opt/bin/python3 "$BASE/home_probe.py" --config "$CONFIG" --mode "$mode"
}

started="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "$started HOME_PROBE_RUN start mode=$mode" >> "$LOG"
set +e
(run_probe) >> "$LOG" 2>&1
probe_rc=$?
set -e
if [ "$probe_rc" -eq 75 ]; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') HOME_PROBE_RUN resource guard skip" >> "$LOG"
  exit 0
fi
if [ "$probe_rc" -ne 0 ]; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') HOME_PROBE_RUN probe failed rc=$probe_rc" >> "$LOG"
  exit "$probe_rc"
fi

if ! nice -n 15 /opt/bin/python3 "$BASE/upload_home_report.py" --config "$CONFIG" >> "$LOG" 2>&1; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') HOME_PROBE_RUN upload failed; local report retained" >> "$LOG"
  exit 3
fi
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') HOME_PROBE_RUN complete" >> "$LOG"
