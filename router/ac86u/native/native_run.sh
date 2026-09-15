#!/bin/sh
# Cron entry: no resident process; idle windows do not start curl or the sampler.
set -eu
umask 077
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
export PATH=/opt/bin:/opt/sbin:/usr/sbin:/usr/bin:/sbin:/bin
IPTV_NATIVE_BASE=/opt/share/iptv-home-native
IPTV_NATIVE_DATA=/opt/var/lib/iptv-home-native
export IPTV_NATIVE_BASE IPTV_NATIVE_DATA
[ -f "$IPTV_NATIVE_DATA/ENABLED" ] || exit 0
[ ! -f "$IPTV_NATIVE_DATA/PAUSED" ] || exit 0
[ "$(date +%z)" = '+0800' ] || exit 2
case "$(date +%H)" in
  02|03|04|05|06|07|08|09|10|13|14|15|20|21|22) ;;
  *) [ -f "$IPTV_NATIVE_DATA/outbox.native" ] || exit 0;;
esac
# The native guardian holds both worker locks before creating a child.
exec "$IPTV_NATIVE_BASE/iptv-native" guard "$IPTV_NATIVE_DATA/resources.txt" \
  59392 51200 16384 240 /bin/sh "$IPTV_NATIVE_BASE/native_worker.sh"
