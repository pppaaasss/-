#!/bin/sh
# One short-lived worker, with shell-visible pause/runtime failure state.
set -eu
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
iptv_data=/opt/var/lib/iptv-home-thin
[ ! -f "$iptv_data/PAUSED" ] || exit 0
[ -f /opt/etc/iptv-home-thin.json ] || exit 0
# Outside household windows only retry a durable upload. No all-day Python polling.
case "$(date +%H)" in
  02|03|04|05|06|07|13|14|15|20|21|22) ;;
  *) [ -f "$iptv_data/outbox.json" ] || [ -f "$iptv_data/working.json" ] || exit 0;;
esac
[ "$(date +%z)" = '+0800' ] || { echo 'THIN_TIMEZONE_ERROR'; exit 2; }
mkdir -p "$iptv_data"
# Stops repeated interpreter failures; status works even if importing json fails.
[ ! -f "$iptv_data/runtime-failed" ] || exit 2
if nice -n 15 /opt/bin/python3 -E -s -B -X utf8 /opt/share/iptv-home-thin/thin_probe.py > "$iptv_data/last-run.log" 2>&1; then
  exit 0
else
  iptv_rc=$?
  # API/validation errors are retryable at the next wake; import failure is not.
  if grep -Eq 'bad magic number|UnicodeDecodeError|ModuleNotFoundError|^ImportError:' "$iptv_data/last-run.log"; then
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$iptv_data/runtime-failed"
  fi
  exit "$iptv_rc"
fi
