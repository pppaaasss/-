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
  python3 ffprobe curl git ca-bundle ca-certificates \
  openssh-client openssh-client-utils openssh-keygen

stage="/opt/tmp/iptv-home-probe-install.$$"
mkdir -p "$stage" "$BASE" "$KEY_DIR" "$DATA" "$LOG_DIR" /opt/var/run
cleanup_stage() {
  case "$stage" in
    /opt/tmp/iptv-home-probe-install.*) rm -rf "$stage" ;;
  esac
}
trap cleanup_stage EXIT HUP INT TERM

files="home_probe.py home_contract.py home_decision.py push_home_report.py github_pair.py activate.py run.sh status.sh uninstall.sh"
for name in $files; do
  /opt/bin/curl -fL --retry 3 --connect-timeout 15 --max-time 120 \
    "$RAW/$name" -o "$stage/$name"
done
/opt/bin/python3 -m py_compile \
  "$stage/home_probe.py" "$stage/home_contract.py" "$stage/home_decision.py" \
  "$stage/push_home_report.py" "$stage/github_pair.py" "$stage/activate.py"
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
    "candidate_manifest_url": "https://raw.githubusercontent.com/pppaaasss/-/master/harvest/home-candidates.json",
    "schedule_timezone": "Asia/Shanghai",
    "expected_utc_offset": "+0800",
    "route_context": "router-origin-direct-wan",
    "actionable": False,
    "github_push_enabled": False,
    "protected_publishing_ready": False,
    "github_repository": "pppaaasss/-",
    "github_report_branch": "home-reports",
    "github_deploy_private_key": "/opt/etc/iptv-home-probe/github_report_ed25519",
    "github_known_hosts": "/opt/etc/iptv-home-probe/github_known_hosts",
    "git": "/opt/bin/git",
    "ssh": "/opt/bin/ssh",
    "ssh_keyscan": "/opt/bin/ssh-keyscan",
    "ssh_keygen": "/opt/bin/ssh-keygen",
    "ffprobe": "/opt/bin/ffprobe",
    "maximum_load1": 1.5,
    "minimum_mem_available_kib": 65536,
    "maximum_runtime_s": 3300,
    "primary_sample_bytes": 2097152,
    "recheck_sample_bytes": 2097152,
    "candidate_sample_bytes": 6291456,
    "candidate_manifest_max_age_hours": 48,
    "candidate_unknown_retry_runs": 2,
    "minimum_sample_bytes": 65536,
    "minimum_headroom_ratio": 1.35,
    "minimum_height_default": 1080,
    "minimum_height_overrides": {"CCTV-4K": 2160},
    "minimum_h264_stream_mbps": 5.0,
    "minimum_hevc_stream_mbps": 2.5,
    "minimum_other_stream_mbps": 3.0,
    "qualified_backup_ttl_hours": 36,
    "backup_refresh_before_hours": 18,
    "circuit_breaker_min_unknown": 12,
    "circuit_breaker_unknown_ratio": 0.35
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
fi
/opt/bin/python3 - "$CONFIG" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value.setdefault("github_push_enabled", False)
value.setdefault("protected_publishing_ready", False)
value.setdefault("github_repository", "pppaaasss/-")
value.setdefault("github_report_branch", "home-reports")
value.setdefault("github_deploy_private_key", "/opt/etc/iptv-home-probe/github_report_ed25519")
value.setdefault("github_known_hosts", "/opt/etc/iptv-home-probe/github_known_hosts")
value.setdefault("git", "/opt/bin/git")
for old in ("upload_enabled", "upload_host", "upload_port", "upload_user", "ssh_private_key", "ssh_known_hosts"):
    value.pop(old, None)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.chmod(0o600)
os.replace(temporary, path)
PY
chmod 0700 "$KEY_DIR" "$DATA"
chmod 0600 "$CONFIG"
if [ ! -f "$KEY_DIR/github_report_ed25519" ]; then
  /opt/bin/ssh-keygen -q -t ed25519 -N '' -C "iptv-home-report-deploy-key" -f "$KEY_DIR/github_report_ed25519"
fi
chmod 0600 "$KEY_DIR/github_report_ed25519"
chmod 0644 "$KEY_DIR/github_report_ed25519.pub"

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
cru a IPTVHomePrimary "0 2 * * * /opt/share/iptv-home-probe/run.sh --run-kind primary-0200"
cru a IPTVHomeRecheck "0 13 * * * /opt/share/iptv-home-probe/run.sh --run-kind recheck-1300"
# END IPTV_HOME_PROBE
EOF
mv -f "$services_tmp" "$SERVICES_START"
chmod 0755 "$SERVICES_START"
cru d IPTVHomeProbe >/dev/null 2>&1 || true
cru d IPTVHomePrimary >/dev/null 2>&1 || true
cru d IPTVHomeRecheck >/dev/null 2>&1 || true
cru a IPTVHomePrimary "0 2 * * * $BASE/run.sh --run-kind primary-0200"
cru a IPTVHomeRecheck "0 13 * * * $BASE/run.sh --run-kind recheck-1300"

echo
echo "Installed in safe local-shadow mode. No playlist or routing rule was changed."
echo "Repository-scoped GitHub deploy public key (add with write access only to pppaaasss/-):"
cat "$KEY_DIR/github_report_ed25519.pub"
echo
echo "Status:  $BASE/status.sh"
echo "Run primary now: $BASE/run.sh --run-kind primary-0200"
echo "Run recheck now: $BASE/run.sh --run-kind recheck-1300"
echo "Do not grant write access or run github_pair.py --enable until protected publishing is ready."
echo "GitHub SSH uses port 443 and the pinned official Ed25519 fingerprint; no home port is opened."

route_context="$(/opt/bin/python3 - "$CONFIG" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("route_context", ""))
PY
)"
if [ "${IPTV_HOME_SKIP_INITIAL_RUN:-0}" != "1" ] && [ "$route_context" = "living-room-path-equivalent" ]; then
  "$BASE/run.sh" --run-kind recheck-1300
  "$BASE/status.sh"
elif [ "$route_context" != "living-room-path-equivalent" ]; then
  echo "Initial probe skipped until the Apple TV-equivalent route is confirmed."
fi
