#!/bin/sh
# One phone command: stage the pinned files, then let the router finish its batch.
set -eu
iptv_ref="${1:?Pass the published release commit SHA}"
case "$iptv_ref" in *[!0-9a-f]*|'') echo 'Invalid SHA'; exit 2;; esac
[ "${#iptv_ref}" -eq 40 ] || exit 2
iptv_stage="$(mktemp -d)"
trap 'rm -rf "$iptv_stage"' EXIT HUP INT TERM
iptv_files='candidate_history.py candidate_delivery.py progress_journal.py home_probe.py daily_worker.py home_contract.py home_decision.py home_resources.py home_transport.py peak_policy.py push_home_report.py github_pair.py activate.py run.sh status.sh runtime_status.py runtime_audit.sh manual_tv_scan.py background_upgrade.py'
iptv_fetch_file() {
  iptv_name="$1"
  iptv_target="$2"
  iptv_raw_url="https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/$iptv_name"
  iptv_api_url="https://api.github.com/repos/pppaaasss/-/contents/router/ac86u/$iptv_name?ref=$iptv_ref"
  if curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 \
    "$iptv_raw_url" -o "$iptv_target"; then
    return 0
  fi
  echo "raw GitHub download failed; retrying through the GitHub API: $iptv_name" >&2
  curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 \
    -H 'Accept: application/vnd.github.raw+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "$iptv_api_url" -o "$iptv_target"
}
for iptv_name in $iptv_files; do
  iptv_fetch_file "$iptv_name" "$iptv_stage/$iptv_name"
done
tar -czf "$iptv_stage/runtime.tar.gz" -C "$iptv_stage" $iptv_files
ssh -p 22 -o ConnectTimeout=10 wodeluyouqi@192.168.50.1 "
set -eu
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
iptv_bundle=/opt/tmp/iptv-stable-$iptv_ref
mkdir -p \"\$iptv_bundle\"
tar -xzf - -C \"\$iptv_bundle\"
/opt/bin/python3 -I -X utf8 \"\$iptv_bundle/background_upgrade.py\" --launch $iptv_ref --bundle \"\$iptv_bundle\"
" < "$iptv_stage/runtime.tar.gz"
echo '更新已交给路由器后台执行。完成标志：BACKGROUND_READY；无需开启白天测速。'
