#!/bin/sh
set -eu

BASE="/opt/share/iptv-home-probe"
CONFIG="/opt/etc/iptv-home-probe.json"
KEY_DIR="/opt/etc/iptv-home-probe"
DATA="/opt/var/lib/iptv-home-probe"
LOG_DIR="/opt/var/log"
SERVICES_START="/jffs/scripts/services-start"
REF="${IPTV_HOME_REF:-master}"
RAW="https://raw.githubusercontent.com/pppaaasss/-/$REF/router/ac86u"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as the router administrator." >&2
  exit 1
fi
if [ ! -x /opt/bin/opkg ]; then
  echo "Entware is not mounted at /opt. Format the USB as ext4 and install Entware with amtm first." >&2
  exit 2
fi
available_kib="$(df -Pk /opt | awk 'NR==2 {print $4}')"
case "$available_kib" in
  ''|*[!0-9]*) echo "Could not verify free USB space." >&2; exit 2 ;;
esac
if [ "$available_kib" -lt 524288 ]; then
  echo "Less than 512 MiB is free on /opt; refusing to install." >&2
  exit 2
fi

/opt/bin/opkg update
/opt/bin/opkg install \
  python3 ffprobe curl ca-bundle ca-certificates \
  openssh-client openssh-client-utils openssh-keygen

stage="/opt/tmp/iptv-home-probe-install.$$"
mkdir -p "$stage" "$BASE" "$KEY_DIR" "$DATA" "$LOG_DIR" /opt/var/run
trap 'find "$stage" -depth -delete 2>/dev/null || true' EXIT HUP INT TERM

files="home_probe.py upload_home_report.py pair.py activate.py run.sh status.sh activate.sh uninstall.sh"
for name in $files; do
  /opt/bin/curl -fL --retry 3 --connect-timeout 15 --max-time 120 \
    "$RAW/$name" -o "$stage/$name"
done
/opt/bin/python3 -m py_compile \
  "$stage/home_probe.py" "$stage/upload_home_report.py" "$stage/pair.py" "$stage/activate.py"
for name in $files; do
  cp -f "$stage/$name" "$BASE/$name"
  chmod 0755 "$BASE/$name"
done

if [ ! -f "$CONFIG" ]; then
  identity="$(nvram get et0macaddr 2>/dev/null || true)"
  [ -n "$identity" ] || identity="$(date +%s)-$$"
  suffix="$(printf '%s' "$identity" | cksum | awk '{print $1}')"
  probe_id="home-ac86u-$suffix"
  IPTV_HOME_PROBE_ID="$probe_id" /opt/bin/python3 - "$CONFIG" "$DATA" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "probe_id": os.environ["IPTV_HOME_PROBE_ID"],
    "output_dir": sys.argv[2],
    "playlist_url": "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u",
    "candidate_playlist_url": "https://raw.githubusercontent.com/pppaaasss/-/master/candidate/tv-core.m3u",
    "route_context": "router-origin-direct-wan",
    "upload_enabled": False,
    "actionable": False,
    "upload_host": "",
    "upload_port": 22,
    "upload_user": "iptv-home-probe",
    "ssh": "/opt/bin/ssh",
    "ssh_keyscan": "/opt/bin/ssh-keyscan",
    "ssh_keygen": "/opt/bin/ssh-keygen",
    "ssh_private_key": "/opt/etc/iptv-home-probe/id_ed25519",
    "ssh_known_hosts": "/opt/etc/iptv-home-probe/known_hosts",
    "ffprobe": "/opt/bin/ffprobe",
    "maximum_load1": 1.5,
    "minimum_mem_available_kib": 65536,
    "maximum_runtime_s": 3300,
    "deep_interval_hours": 72,
    "light_sample_bytes": 2097152,
    "deep_sample_bytes": 12582912,
    "minimum_sample_bytes": 65536,
    "minimum_headroom_ratio": 1.35,
    "minimum_height_default": 1080,
    "minimum_height_overrides": {"CCTV-4K": 2160},
    "minimum_h264_stream_mbps": 5.0,
    "dead_after_runs": 3,
    "dead_min_age_hours": 6,
    "degraded_after_runs": 3,
    "degraded_min_age_hours": 6,
    "candidate_good_after_runs": 2,
    "candidate_good_min_age_hours": 6,
    "circuit_breaker_min_unknown": 12,
    "circuit_breaker_unknown_ratio": 0.35,
    "quality_cache_hours": 96
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
fi
chmod 0700 "$KEY_DIR" "$DATA"
chmod 0600 "$CONFIG"
if [ ! -f "$KEY_DIR/id_ed25519" ]; then
  /opt/bin/ssh-keygen -q -t ed25519 -N '' -C "iptv-home-probe" -f "$KEY_DIR/id_ed25519"
fi
chmod 0600 "$KEY_DIR/id_ed25519"
chmod 0644 "$KEY_DIR/id_ed25519.pub"

mkdir -p /jffs/scripts
services_tmp="$SERVICES_START.iptv-home.$$"
if [ -f "$SERVICES_START" ]; then
  awk '
    /^# BEGIN IPTV_HOME_PROBE$/ {skip=1; next}
    /^# END IPTV_HOME_PROBE$/ {skip=0; next}
    !skip {print}
  ' "$SERVICES_START" > "$services_tmp"
else
  printf '%s\n' '#!/bin/sh' > "$services_tmp"
fi
cat >> "$services_tmp" <<'EOF'
# BEGIN IPTV_HOME_PROBE
cru a IPTVHomeProbe "7 */6 * * * /opt/share/iptv-home-probe/run.sh"
# END IPTV_HOME_PROBE
EOF
mv -f "$services_tmp" "$SERVICES_START"
chmod 0755 "$SERVICES_START"
cru d IPTVHomeProbe >/dev/null 2>&1 || true
cru a IPTVHomeProbe "7 */6 * * * $BASE/run.sh"

echo
echo "Installed in safe local-shadow mode. No playlist or routing rule was changed."
echo "Probe public key (use only for the restricted VPS receiver):"
cat "$KEY_DIR/id_ed25519.pub"
echo
echo "Status:  $BASE/status.sh"
echo "Run now: $BASE/run.sh --mode light"
echo "Pairing remains disabled until a pinned VPS host fingerprint is supplied."

if [ "${IPTV_HOME_SKIP_INITIAL_RUN:-0}" != "1" ]; then
  "$BASE/run.sh" --mode light
  "$BASE/status.sh"
fi
