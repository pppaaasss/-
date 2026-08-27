#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/pppaaasss/-.git"
INSTALL_DIR="/opt/iptv-hk-probe"
DATA_DIR="/var/lib/iptv-hk-probe"
LOG_FILE="/var/log/iptv-hk-probe.log"
CRON_FILE="/etc/cron.d/iptv-hk-probe"
LOCK_FILE="/run/iptv-hk-probe.lock"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_hk_probe.sh" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 ffmpeg ca-certificates curl util-linux
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache git python3 ffmpeg ca-certificates curl util-linux
else
  echo "Unsupported package manager. Need git python3 ffprobe flock." >&2
  exit 1
fi

mkdir -p "$DATA_DIR" "$DATA_DIR/candidates"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --quiet origin master
  git -C "$INSTALL_DIR" reset --hard origin/master
else
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

chmod +x "$INSTALL_DIR/scripts/hk_probe.py" "$INSTALL_DIR/scripts/hk_filter_harvest.py"

cat > /usr/local/sbin/iptv-hk-probe <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0
cd "$INSTALL_DIR"
git fetch --quiet origin master || true
git reset --hard origin/master >/dev/null 2>&1 || true

# 1) Audit the frozen formal core lineup from Hong Kong.
python3 scripts/hk_probe.py \
  --playlist tv-core.m3u \
  --output-dir "$DATA_DIR"

# 2) Screen GitHub-harvested spare routes from Hong Kong. The harvest is raw
#    text only; all real stream judgement happens here.
if [[ -s harvest/candidates.jsonl ]]; then
  python3 scripts/hk_filter_harvest.py \
    --harvest harvest/candidates.jsonl \
    --playlist tv-core.m3u \
    --feedback config/home-route-feedback.json \
    --output-dir "$DATA_DIR/candidates" \
    --workers 12 \
    --max-per-channel 14
else
  echo 'harvest/candidates.jsonl not present yet; skipping spare-route filter'
fi
EOF
chmod +x /usr/local/sbin/iptv-hk-probe

cat > /usr/local/sbin/iptv-hk-filter <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
git fetch --quiet origin master || true
git reset --hard origin/master >/dev/null 2>&1 || true
exec python3 scripts/hk_filter_harvest.py \
  --harvest harvest/candidates.jsonl \
  --playlist tv-core.m3u \
  --feedback config/home-route-feedback.json \
  --output-dir "$DATA_DIR/candidates" \
  "\$@"
EOF
chmod +x /usr/local/sbin/iptv-hk-filter

cat > "$CRON_FILE" <<'EOF'
# Hong Kong IPTV judge: every 3 hours at minute 17.
# GitHub harvests source text only; Hong Kong performs stream probing.
# It never edits production playlists and never auto-switches channels.
17 */3 * * * root /usr/local/sbin/iptv-hk-probe >> /var/log/iptv-hk-probe.log 2>&1
EOF
chmod 0644 "$CRON_FILE"

# First run now so installation failures are visible immediately.
/usr/local/sbin/iptv-hk-probe | tee -a "$LOG_FILE"

echo
echo "Installed."
echo "Formal report:     $DATA_DIR/latest.txt"
echo "Formal JSON:       $DATA_DIR/latest.json"
echo "Formal state:      $DATA_DIR/state.json"
echo "Spare report:      $DATA_DIR/candidates/latest.txt"
echo "Spare JSON:        $DATA_DIR/candidates/latest.json"
echo "Spare M3U:         $DATA_DIR/candidates/latest.m3u"
echo "Log:               $LOG_FILE"
echo "Cron:              every 3 hours at minute 17"
