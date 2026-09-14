#!/bin/sh
# Intentionally does not import Python, parse full history, or contact GitHub.
iptv_data=/opt/var/lib/iptv-home-thin
for iptv_name in PAUSED runtime-failed status.json error.json; do
  if [ -f "$iptv_data/$iptv_name" ]; then
    printf '%s\n' "$iptv_name:"
    head -c 4096 "$iptv_data/$iptv_name"
    printf '\n'
  fi
done
printf '%s\n' 'memory:'
awk '/^(MemAvailable|MemFree|SwapFree):/ {print}' /proc/meminfo
printf '%s\n' 'IPTV cron:'
cru l 2>/dev/null | grep -E '#IPTVHome(Thin|Primary|Recheck|Peak|Resume)#' || true
printf '%s\n' 'last run:'
tail -n 8 "$iptv_data/last-run.log" 2>/dev/null || true
