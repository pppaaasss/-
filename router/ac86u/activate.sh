#!/bin/sh
set -eu
BASE="/opt/share/iptv-home-probe"

if [ "${1:-}" = "--off" ]; then
  exec /opt/bin/python3 "$BASE/activate.py" --off
fi
if [ "${1:-}" = "--confirm-living-room-path" ]; then
  /opt/bin/python3 "$BASE/activate.py" --confirm-living-room-path
else
  /opt/bin/python3 "$BASE/activate.py"
fi
exec "$BASE/run.sh" --mode light
