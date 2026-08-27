#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/pppaaasss/-.git"
INSTALL_DIR="/opt/iptv-hk-probe"
DATA_DIR="/var/lib/iptv-hk-probe"
LOG_FILE="/var/log/iptv-hk-probe.log"
CRON_FILE="/etc/cron.d/iptv-hk-probe"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_hk_probe.sh" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 ffmpeg ca-certificates curl
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache git python3 ffmpeg ca-certificates curl
else
  echo "Unsupported package manager. Need git python3 ffprobe." >&2
  exit 1
fi

mkdir -p "$DATA_DIR"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --quiet origin master
  git -C "$INSTALL_DIR" reset --hard origin/master
else
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

chmod +x "$INSTALL_DIR/scripts/hk_probe.py"

cat > /usr/local/sbin/iptv-hk-probe <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
git fetch --quiet origin master || true
git reset --hard origin/master >/dev/null 2>&1 || true
exec python3 scripts/hk_probe.py --playlist tv-core.m3u --output-dir "$DATA_DIR"
EOF
chmod +x /usr/local/sbin/iptv-hk-probe

cat > "$CRON_FILE" <<'EOF'
# IPTV Hong Kong probe: every 3 hours at minute 17.
# It never edits production playlists and does not auto-switch channels.
17 */3 * * * root /usr/local/sbin/iptv-hk-probe >> /var/log/iptv-hk-probe.log 2>&1
EOF
chmod 0644 "$CRON_FILE"

# First run now so installation failures are visible immediately.
/usr/local/sbin/iptv-hk-probe | tee -a "$LOG_FILE"

echo
echo "Installed."
echo "Report: $DATA_DIR/latest.txt"
echo "JSON:   $DATA_DIR/latest.json"
echo "State:  $DATA_DIR/state.json"
echo "Log:    $LOG_FILE"
echo "Cron:   every 3 hours at minute 17"
