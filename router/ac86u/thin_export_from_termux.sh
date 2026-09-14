#!/bin/sh
# Run on the phone. Export a frozen snapshot without importing router Python.
set -eu
umask 077
iptv_export=${1:?Pass a new local backup directory}
[ ! -e "$iptv_export" ] || { echo 'Export directory already exists'; exit 2; }
mkdir -p "$iptv_export"
ssh -p 22 -o ConnectTimeout=10 wodeluyouqi@192.168.50.1 'sh -s' > "$iptv_export/history.tar" <<'REMOTE'
set -eu
cd /opt/var/lib/iptv-home-probe
[ -f thin-mode.locked ] || { echo 'Legacy task must be frozen first' >&2; exit 2; }
[ -f state.json ] || { echo 'Required state.json missing' >&2; exit 2; }
for p in state.json.corrupt-* pipeline-trial/state.json.corrupt-* qualified-backups.json.corrupt-*; do
  [ ! -e "$p" ] || { echo 'Unresolved quarantined history; recover it before migration' >&2; exit 2; }
done
set -- state.json
for p in qualified-backups.json pipeline-trial/state.json pipeline-trial/qualified-backups.json progress.jsonl; do
  if [ -f "$p" ]; then set -- "$@" "$p"; fi
done
tar -cf - "$@"
REMOTE
# Controlled filenames come from the fixed remote allowlist above.
tar -xf "$iptv_export/history.tar" -C "$iptv_export"
ssh -p 22 -o ConnectTimeout=10 wodeluyouqi@192.168.50.1 \
  'cat /opt/var/lib/iptv-home-thin/migration/quality-policy.json' > "$iptv_export/quality-policy.json"
printf '%s\n' '历史副本已保存到手机。上传脚本仅发送列出的历史文件和画质策略。'
