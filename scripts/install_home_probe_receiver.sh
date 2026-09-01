#!/usr/bin/env bash
set -euo pipefail

PROBE_ID=""
PUBLIC_KEY=""
DATA_DIR="/var/lib/iptv-hk-probe/home"
ACCOUNT_HOME="/var/lib/iptv-home-probe-receiver"
ACCOUNT="iptv-home-probe"
LIB_DIR="/usr/local/lib/iptv-home-probe"
WRAPPER="/usr/local/sbin/iptv-home-receive"

echo "RETIRED: home reports go directly from AC86U to the isolated GitHub report branch." >&2
echo "The Hong Kong SSH receiver is no longer part of the supported path." >&2
exit 2

while [[ $# -gt 0 ]]; do
  case "$1" in
    --probe-id) PROBE_ID="${2:-}"; shift 2 ;;
    --public-key) PUBLIC_KEY="${2:-}"; shift 2 ;;
    --public-key-file) PUBLIC_KEY="$(<"${2:-}")"; shift 2 ;;
    --data-dir) DATA_DIR="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root on the Hong Kong VPS." >&2
  exit 1
fi
if [[ ! "$PROBE_ID" =~ ^[a-z0-9][a-z0-9-]{2,31}$ ]]; then
  echo "--probe-id is missing or invalid." >&2
  exit 2
fi
if [[ ! "$PUBLIC_KEY" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
  echo "--public-key must be one Ed25519 public key from the router." >&2
  exit 2
fi
if [[ "$DATA_DIR" != /var/lib/iptv-hk-probe/home ]]; then
  echo "For safety this installer only manages /var/lib/iptv-hk-probe/home." >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 openssh-server
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 openssh-server
  else
    echo "Install Python 3 and OpenSSH server first." >&2
    exit 3
  fi
fi

if ! id "$ACCOUNT" >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$ACCOUNT_HOME" --shell /bin/sh "$ACCOUNT"
  else
    adduser -S -D -h "$ACCOUNT_HOME" -s /bin/sh "$ACCOUNT"
  fi
fi
passwd -l "$ACCOUNT" >/dev/null 2>&1 || true
mkdir -p "$LIB_DIR" "$DATA_DIR/history" "$ACCOUNT_HOME/.ssh"
install -m 0755 scripts/receive_home_probe.py "$LIB_DIR/receive_home_probe.py"
install -m 0644 scripts/home_probe_report.py "$LIB_DIR/home_probe_report.py"
python3 -m py_compile "$LIB_DIR/receive_home_probe.py" "$LIB_DIR/home_probe_report.py"

cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$LIB_DIR/receive_home_probe.py" \\
  --expected-probe-id "$PROBE_ID" \\
  --output-dir "$DATA_DIR" \\
  --history-limit 40
EOF
chmod 0755 "$WRAPPER"

forced="restrict,command=\"$WRAPPER\" $PUBLIC_KEY"
printf '%s\n' "$forced" > "$ACCOUNT_HOME/.ssh/authorized_keys"
chmod 0700 "$ACCOUNT_HOME/.ssh"
chmod 0600 "$ACCOUNT_HOME/.ssh/authorized_keys"
chown -R "$ACCOUNT:$ACCOUNT" "$ACCOUNT_HOME" "$DATA_DIR"

echo "Restricted home-probe receiver installed."
echo "Account:     $ACCOUNT (key only; forced command; no forwarding or PTY)"
echo "Destination: $DATA_DIR/latest.json"
echo "Probe ID:    $PROBE_ID"
echo "Verify this VPS fingerprint before pairing the router:"
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
