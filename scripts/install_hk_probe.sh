#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/pppaaasss/-.git"
INSTALL_DIR="/opt/iptv-hk-probe"
DATA_DIR="/var/lib/iptv-hk-probe"
LOG_FILE="/var/log/iptv-hk-probe.log"
CRON_FILE="/etc/cron.d/iptv-hk-probe"
LOCK_FILE="/run/iptv-hk-probe.lock"
TOKEN_FILE="/etc/iptv-hk-probe.github-token"
ASKPASS="/usr/local/libexec/iptv-hk-git-askpass"

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

mkdir -p "$DATA_DIR" "$DATA_DIR/formal" "$DATA_DIR/candidates" "$(dirname "$ASKPASS")"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --quiet origin master
  git -C "$INSTALL_DIR" reset --hard origin/master
else
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

chmod +x \
  "$INSTALL_DIR/scripts/hk_probe.py" \
  "$INSTALL_DIR/scripts/hk_filter_harvest.py" \
  "$INSTALL_DIR/scripts/hk_auto_update.py" \
  "$INSTALL_DIR/scripts/hk_cycle.sh"

# Optional write credential. Pass GITHUB_TOKEN only on the server when running
# this installer; never commit it to the repository.
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  umask 077
  printf '%s' "$GITHUB_TOKEN" > "$TOKEN_FILE"
  chmod 0600 "$TOKEN_FILE"
fi

cat > "$ASKPASS" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) cat /etc/iptv-hk-probe.github-token 2>/dev/null || true ;;
  *) printf '\n' ;;
esac
EOF
chmod 0700 "$ASKPASS"

cat > /usr/local/sbin/iptv-hk-probe <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0
export GIT_TERMINAL_PROMPT=0
if [[ -s "$TOKEN_FILE" ]]; then
  export GIT_ASKPASS="$ASKPASS"
fi
exec "$INSTALL_DIR/scripts/hk_cycle.sh"
EOF
chmod +x /usr/local/sbin/iptv-hk-probe

cat > /usr/local/sbin/iptv-hk-filter <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
exec python3 scripts/hk_filter_harvest.py \
  --harvest harvest/candidates.jsonl \
  --playlist tv-core.m3u \
  --feedback config/home-route-feedback.json \
  --output-dir "$DATA_DIR/candidates" \
  "\$@"
EOF
chmod +x /usr/local/sbin/iptv-hk-filter

cat > "$CRON_FILE" <<'EOF'
# Hong Kong IPTV judge + automatic formal-route repair every 3 hours.
# GitHub only harvests source text; all stream probing/selection happens in HK.
17 */3 * * * root /usr/local/sbin/iptv-hk-probe >> /var/log/iptv-hk-probe.log 2>&1
EOF
chmod 0644 "$CRON_FILE"

# First run now. Without a GitHub write credential, probing still works and any
# attempted formal change is reverted if push authentication fails.
/usr/local/sbin/iptv-hk-probe | tee -a "$LOG_FILE"

echo
echo "Installed."
echo "Formal report:     $DATA_DIR/formal/latest.txt"
echo "Formal JSON:       $DATA_DIR/formal/latest.json"
echo "Spare report:      $DATA_DIR/candidates/latest.txt"
echo "Spare JSON:        $DATA_DIR/candidates/latest.json"
echo "Auto-update:       $DATA_DIR/auto-update-summary.json"
echo "Log:               $LOG_FILE"
echo "Cron:              every 3 hours at minute 17"
if [[ -s "$TOKEN_FILE" ]]; then
  echo "GitHub auto-push:   enabled"
else
  echo "GitHub auto-push:   waiting for write credential ($TOKEN_FILE)"
fi
