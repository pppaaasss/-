#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/pppaaasss/-.git"
INSTALL_DIR="/opt/iptv-hk-probe"
DATA_DIR="/var/lib/iptv-hk-probe"
LOG_FILE="/var/log/iptv-hk-probe.log"
CRON_FILE="/etc/cron.d/iptv-hk-probe"
SERVICE_FILE="/etc/systemd/system/iptv-hk-probe.service"
TIMER_FILE="/etc/systemd/system/iptv-hk-probe.timer"
LOCK_FILE="/run/iptv-hk-probe.lock"
TOKEN_FILE="/etc/iptv-hk-probe.github-token"
ASKPASS="/usr/local/libexec/iptv-hk-git-askpass"

echo "RETIRED: do not install the Hong Kong IPTV monitor; AC86U is the sole health authority." >&2
echo "The legacy installer remains below only as rollback evidence." >&2
exit 2

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_hk_probe.sh" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 ffmpeg ca-certificates curl util-linux cron
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache git python3 ffmpeg ca-certificates curl util-linux dcron
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
  "$INSTALL_DIR/scripts/dead_only_failover.py" \
  "$INSTALL_DIR/scripts/publish_hk_health.py" \
  "$INSTALL_DIR/scripts/hk_cycle.sh"

# A write credential is mandatory because a run is not healthy until its report
# is visible on health-monitor. Never commit this token to the repository.
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  previous_umask="$(umask)"
  umask 077
  printf '%s' "$GITHUB_TOKEN" > "$TOKEN_FILE"
  chmod 0600 "$TOKEN_FILE"
  umask "$previous_umask"
  unset previous_umask
fi
if [[ ! -s "$TOKEN_FILE" ]]; then
  if [[ -t 0 ]]; then
    echo "Paste a GitHub token with repository Contents read/write permission."
    printf 'GitHub token: '
    IFS= read -r -s token
    echo
    if [[ -n "${token:-}" ]]; then
      install -m 0600 /dev/null "$TOKEN_FILE"
      printf '%s' "$token" > "$TOKEN_FILE"
      unset token
    fi
  fi
fi
if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "Missing required GitHub token: $TOKEN_FILE" >&2
  exit 2
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
# Run through bash as a second guard. A repository checkout must never turn a
# harmless executable-bit drift into systemd status=126.
exec bash "$INSTALL_DIR/scripts/hk_cycle.sh"
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

# Prove the complete probe + health upload path before enabling a schedule.
# pipefail makes a failed upload fail the installer instead of looking healthy.
if ! /usr/local/sbin/iptv-hk-probe 2>&1 | tee -a "$LOG_FILE"; then
  echo "Initial probe or GitHub health upload failed; scheduler not enabled." >&2
  exit 3
fi

SCHEDULER=""
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hong Kong IPTV health probe and confirmed-dead failover
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/iptv-hk-probe
TimeoutStartSec=3h
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
EOF

  cat > "$TIMER_FILE" <<'EOF'
[Unit]
Description=Run Hong Kong IPTV health probe every six hours

[Timer]
OnCalendar=*-*-* 00/6:17:00
Persistent=true
Unit=iptv-hk-probe.service

[Install]
WantedBy=timers.target
EOF
  rm -f "$CRON_FILE"
  systemctl daemon-reload
  systemctl enable --now iptv-hk-probe.timer
  systemctl restart iptv-hk-probe.timer
  systemctl is-enabled --quiet iptv-hk-probe.timer
  systemctl is-active --quiet iptv-hk-probe.timer
  SCHEDULER="systemd timer at 00:17/06:17/12:17/18:17"
else
  cat > "$CRON_FILE" <<'EOF'
# Hong Kong IPTV monitor every 6 hours.
# Production changes are allowed only for repeatedly confirmed DEAD routes.
17 */6 * * * root /usr/local/sbin/iptv-hk-probe >> /var/log/iptv-hk-probe.log 2>&1
EOF
  chmod 0644 "$CRON_FILE"
  if command -v rc-service >/dev/null 2>&1; then
    rc-update add crond default >/dev/null 2>&1 || true
    rc-service crond restart
  elif command -v service >/dev/null 2>&1; then
    service cron restart || service crond restart
  else
    echo "No supported scheduler service manager found." >&2
    exit 4
  fi
  if ! pgrep -x cron >/dev/null 2>&1 && ! pgrep -x crond >/dev/null 2>&1; then
    echo "Cron configuration exists but no cron daemon is running." >&2
    exit 5
  fi
  SCHEDULER="cron at minute 17 every six hours"
fi

echo
echo "Installed."
echo "Formal report:     $DATA_DIR/formal/latest.txt"
echo "Formal JSON:       $DATA_DIR/formal/latest.json"
echo "GitHub health:     health-monitor:health/latest.json"
echo "Dead failover:     confirmed DEAD only; fixed + GitHub pending spares"
echo "Log:               $LOG_FILE"
echo "Scheduler:         $SCHEDULER"
echo "Production update: confirmed DEAD routes only"
