#!/bin/sh
set -eu
data=/opt/var/lib/iptv-home-native
for flag in ENABLED PAUSED; do
  if [ -f "$data/$flag" ]; then printf '%s\n' "$flag"; fi
done
for name in status.txt resources.txt; do
  if [ -f "$data/$name" ]; then cat "$data/$name"; fi
done
if [ -f "$data/route-mark" ]; then printf 'PENDING_ROUTE_MARK '; cat "$data/route-mark"; fi
if [ -f "$data/route-error.txt" ]; then tail -n 8 "$data/route-error.txt"; fi
awk '/^(MemAvailable|MemFree|SwapFree):/ {print}' /proc/meminfo
if [ -f "$data/outbox.native" ]; then printf 'PENDING_UPLOAD_BYTES '; wc -c < "$data/outbox.native"; fi
