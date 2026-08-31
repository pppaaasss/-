#!/bin/sh
set -eu
BASE="/opt/share/iptv-home-probe"

if [ "${1:-}" = "--off" ]; then
  exec /opt/bin/python3 "$BASE/activate.py" --off
fi
/opt/bin/python3 "$BASE/activate.py"
exec "$BASE/run.sh" --mode light
