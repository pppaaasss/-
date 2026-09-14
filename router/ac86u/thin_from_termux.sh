#!/bin/sh
# PHONE entry point. Python is used on the phone only to prepare safe migration.
set -eu
umask 077
iptv_ref=${1:?Pass the reviewed 40-character commit SHA}
iptv_token_file=${2:?Pass the phone token file (0600)}
case "$iptv_ref" in *[!0-9a-f]*|'') exit 2;; esac
[ "${#iptv_ref}" -eq 40 ] || exit 2
iptv_stage="$HOME/iptv-native-$iptv_ref"
mkdir -p "$iptv_stage"
for iptv_name in iptv-native build.json iptv_native.c native_from_termux.py native_run.sh native_worker.sh native_status.sh; do
  curl -fsSL --retry 2 --connect-timeout 10 --max-time 90 \
    "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/native/$iptv_name" -o "$iptv_stage/$iptv_name"
done
curl -fsSL --retry 2 --connect-timeout 10 --max-time 90 \
  "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/config/home-thin.json" -o "$iptv_stage/home-thin.json"
python "$iptv_stage/native_from_termux.py" stage --token-file "$iptv_token_file"
printf '%s\n' "手机管理入口：$iptv_stage/native_from_termux.py"
