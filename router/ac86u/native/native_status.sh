#!/bin/sh
set -eu
data=/opt/var/lib/iptv-home-native
for flag in ENABLED PAUSED; do
  if [ -f "$data/$flag" ]; then printf '%s\n' "$flag"; fi
done
for name in status.txt resources.txt; do
  if [ -f "$data/$name" ]; then cat "$data/$name"; fi
done
awk '/^(MemAvailable|MemFree|SwapFree):/ {print}' /proc/meminfo
if [ -f "$data/outbox.native" ]; then printf 'PENDING_UPLOAD_BYTES '; wc -c < "$data/outbox.native"; fi
