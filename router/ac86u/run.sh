#!/bin/sh
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
set -eu

BASE="/opt/share/iptv-home-probe"
CONFIG="/opt/etc/iptv-home-probe.json"
DATA="/opt/var/lib/iptv-home-probe"
LOG="/opt/var/log/iptv-home-probe.log"
LOCK="/opt/var/run/iptv-home-probe.lock"

[ ! -f "$DATA/background-upgrade.locked" ] || exit 0

mkdir -p "$DATA" "$(dirname "$LOG")" "$(dirname "$LOCK")"
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ]; then
  tail -c 1048576 "$LOG" > "$LOG.1"
  : > "$LOG"
fi

# The worker owns a kernel flock released automatically on exit or reboot.
# Leave legacy manual execution available while the new worker is disabled.
if ! daily_enabled="$(/opt/bin/python3 -I -c 'import json; print(json.load(open("/opt/etc/iptv-home-probe.json")).get("daily_worker_enabled") is True)' 2>> "$LOG")"; then
  printf '%s\n' '{"state":"STOPPED_RUNTIME_ERROR","source":"config_reader"}' > "$DATA/daily-runtime-error.json"
  /bin/sh "$BASE/runtime_audit.sh" "$DATA" failure "$LOG" >/dev/null 2>&1 || true
  exit 1
fi
if [ "$daily_enabled" = "True" ]; then
  if [ -f "$DATA/daily-runtime-error.json" ]; then exit 1; fi
  if [ "${1:-}" = "--resume" ]; then
    set --
  elif [ "${1:-}" = "--run-kind" ]; then
    set -- --enqueue "$2"
  else
    set -- --enqueue primary-0200
  fi
  set +e
  nice -n 15 /opt/bin/python3 -E -s -u "$BASE/daily_worker.py" --config "$CONFIG" "$@" >> "$LOG" 2>&1
  daily_rc=$?
  set -e
  if [ "$daily_rc" -ne 0 ] && [ ! -f "$DATA/daily-runtime-error.json" ]; then
    printf '%s\n' '{"state":"STOPPED_RUNTIME_ERROR","source":"worker_startup"}' > "$DATA/daily-runtime-error.json"
    /bin/sh "$BASE/runtime_audit.sh" "$DATA" failure "$LOG" >/dev/null 2>&1 || true
  fi
  exit "$daily_rc"
fi
[ "${1:-}" = "--resume" ] && exit 0
if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT HUP INT TERM


run_kind="primary-0200"
if [ "${1:-}" = "--run-kind" ] && [ -n "${2:-}" ]; then
  run_kind="$2"
fi
case "$run_kind" in
  primary-0200|recheck-1300|peak-2000) ;;
  *) echo "Unsupported run kind: $run_kind" >&2; exit 2 ;;
esac

run_probe() {
  for ionice_path in /opt/bin/ionice /usr/bin/ionice /bin/ionice; do
    if [ -x "$ionice_path" ]; then
      exec "$ionice_path" -c 3 nice -n 15 /opt/bin/python3 "$BASE/home_probe.py" --config "$CONFIG" --run-kind "$run_kind"
    fi
  done
  exec nice -n 15 /opt/bin/python3 "$BASE/home_probe.py" --config "$CONFIG" --run-kind "$run_kind"
}

started="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "$started HOME_PROBE_RUN start run_kind=$run_kind" >> "$LOG"
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
  if [ -f "$BASE/runtime_audit.sh" ]; then
    /bin/sh "$BASE/runtime_audit.sh" "$DATA" failure "$LOG" >/dev/null 2>&1 || true
  fi
  exit "$probe_rc"
fi

if [ -f "$BASE/runtime_audit.sh" ] && [ ! -f "$DATA/runtime-diagnostics/baseline.sha256" ]; then
  /bin/sh "$BASE/runtime_audit.sh" "$DATA" baseline "$LOG" >/dev/null 2>&1 || true
fi

if ! nice -n 15 /opt/bin/python3 "$BASE/push_home_report.py" --config "$CONFIG" >> "$LOG" 2>&1; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') HOME_PROBE_RUN GitHub push failed; local report queued" >> "$LOG"
  exit 3
fi
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') HOME_PROBE_RUN complete" >> "$LOG"
