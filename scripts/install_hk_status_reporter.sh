#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IPTV_REPO_DIR:-/opt/iptv-hk-probe}"
TOKEN_FILE="/etc/iptv-hk-probe.github-token"
ASKPASS="/usr/local/libexec/iptv-hk-git-askpass"
SERVICE="/etc/systemd/system/iptv-hk-status.service"
TIMER="/etc/systemd/system/iptv-hk-status.timer"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

cd "$REPO_DIR"
git fetch origin master
git reset --hard origin/master

if [[ ! -s "$TOKEN_FILE" ]]; then
  echo
  echo "Paste a GitHub fine-grained PAT now. Input will be hidden."
  echo "Required repo permissions: Contents read/write + Issues read/write."
  printf "GitHub token: "
  IFS= read -r -s token
  echo
  if [[ -z "${token:-}" ]]; then
    echo "Empty token; aborting." >&2
    exit 2
  fi
  install -m 600 /dev/null "$TOKEN_FILE"
  printf '%s\n' "$token" > "$TOKEN_FILE"
  unset token
fi

mkdir -p "$(dirname "$ASKPASS")"
cat > "$ASKPASS" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *) cat /etc/iptv-hk-probe.github-token ;;
esac
EOF
chmod 700 "$ASKPASS"

cat > "$SERVICE" <<EOF
[Unit]
Description=Report Hong Kong IPTV probe progress to GitHub Issue #2
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $REPO_DIR/scripts/hk_report_status.py --repo pppaaasss/- --issue 2
EOF

cat > "$TIMER" <<'EOF'
[Unit]
Description=Report Hong Kong IPTV probe status every 10 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=10min
Persistent=true
Unit=iptv-hk-status.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now iptv-hk-status.timer
systemctl start iptv-hk-status.service

echo
systemctl --no-pager --full status iptv-hk-status.service || true
echo
systemctl list-timers --all --no-pager | grep iptv-hk-status || true
echo "HK status reporter installed. GitHub Issue #2 will refresh every 10 minutes."
