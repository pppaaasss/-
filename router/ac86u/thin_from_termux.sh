#!/bin/sh
# Download on the phone; the router only receives a pinned small runtime bundle.
set -eu
umask 077
iptv_ref=${1:?Pass the reviewed 40-character commit SHA}
case "$iptv_ref" in *[!0-9a-f]*|'') exit 2;; esac
[ "${#iptv_ref}" -eq 40 ] || exit 2
iptv_stage="$(mktemp -d)"
trap 'rm -rf "$iptv_stage"' EXIT HUP INT TERM
iptv_files='thin_probe.py thin_api.py thin_contract.py thin_install.py thin_run.sh thin_status.sh home_probe.py home_transport.py home_resources.py home_contract.py home_decision.py candidate_history.py candidate_delivery.py progress_journal.py peak_policy.py'
for iptv_name in $iptv_files; do
  curl -fsSL --retry 2 --connect-timeout 10 --max-time 90 \
    "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/$iptv_name" -o "$iptv_stage/$iptv_name"
done
curl -fsSL --retry 2 --connect-timeout 10 --max-time 90 \
  "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/config/home-thin.json" -o "$iptv_stage/home-thin.json"
tar -czf "$iptv_stage/runtime.tar.gz" -C "$iptv_stage" $iptv_files home-thin.json
ssh -p 22 -o ConnectTimeout=10 wodeluyouqi@192.168.50.1 "
set -eu
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
/opt/bin/python3 -I -S -B -c 'import keyword,json,ssl,pathlib,threading,fcntl; print(\"PYTHON_OK\")'
iptv_bundle=/opt/tmp/iptv-thin-$iptv_ref
mkdir -p \"\$iptv_bundle\"
tar -xzf - -C \"\$iptv_bundle\"
/opt/bin/python3 -I -B \"\$iptv_bundle/thin_install.py\" stage --bundle \"\$iptv_bundle\"
" < "$iptv_stage/runtime.tar.gz"
printf '%s\n' '代码已暂存，旧测速已暂停。接下来迁移历史，确认云端接收后再启用。'
