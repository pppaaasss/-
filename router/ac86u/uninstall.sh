#!/bin/sh
set -eu

BASE="/opt/share/iptv-home-probe"
CONFIG="/opt/etc/iptv-home-probe.json"
KEY_DIR="/opt/etc/iptv-home-probe"
DATA="/opt/var/lib/iptv-home-probe"
SERVICES_START="/jffs/scripts/services-start"
purge=0
[ "${1:-}" = "--purge" ] && purge=1

cru d IPTVHomeProbe >/dev/null 2>&1 || true
if [ -f "$SERVICES_START" ]; then
  temporary="$SERVICES_START.iptv-home.$$"
  awk '
    /^# BEGIN IPTV_HOME_PROBE$/ {skip=1; next}
    /^# END IPTV_HOME_PROBE$/ {skip=0; next}
    !skip {print}
  ' "$SERVICES_START" > "$temporary"
  mv -f "$temporary" "$SERVICES_START"
  chmod 0755 "$SERVICES_START"
fi

if [ "$BASE" = "/opt/share/iptv-home-probe" ] && [ -d "$BASE" ]; then
  find "$BASE" -depth -delete
fi
if [ "$purge" -eq 1 ]; then
  if [ "$KEY_DIR" = "/opt/etc/iptv-home-probe" ] && [ -d "$KEY_DIR" ]; then
    find "$KEY_DIR" -depth -delete
  fi
  [ "$CONFIG" = "/opt/etc/iptv-home-probe.json" ] && rm -f "$CONFIG"
  if [ "$DATA" = "/opt/var/lib/iptv-home-probe" ] && [ -d "$DATA" ]; then
    find "$DATA" -depth -delete
  fi
  echo "Uninstalled and purged the probe key, config, and USB state. Entware packages were kept."
else
  echo "Uninstalled probe code and cron. Config, key, and USB history were kept for recovery."
  echo "Run this script with --purge before uninstalling the code if permanent erasure is wanted."
fi
