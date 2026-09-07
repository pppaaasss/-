#!/bin/sh
# Stage a pinned release on the phone before installing it on the router.
set -eu
iptv_ref="${1:?Pass the release commit SHA}"
case "$iptv_ref" in *[!0-9a-f]*|'') echo 'Invalid commit SHA'; exit 2;; esac
[ "${#iptv_ref}" -eq 40 ] || exit 2
iptv_stage="$(mktemp -d)"
trap 'rm -rf "$iptv_stage"' EXIT HUP INT TERM
for iptv_name in install.sh home_probe.py peak_policy.py daily_worker.py daily_readiness.py home_resources.py home_transport.py transport_check.py home_contract.py home_decision.py push_home_report.py github_pair.py activate.py activate.sh run.sh runtime_audit.sh status.sh uninstall.sh; do
  curl -4 -fSL --retry 3 --connect-timeout 15 --max-time 120 \
    "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/$iptv_name" -o "$iptv_stage/$iptv_name"
done
# Explicit files keep the output archive out of its own input.
tar -czf "$iptv_stage/bundle.tar.gz" -C "$iptv_stage" \
  install.sh home_probe.py peak_policy.py daily_worker.py daily_readiness.py home_resources.py home_transport.py transport_check.py home_contract.py home_decision.py push_home_report.py github_pair.py activate.py activate.sh run.sh runtime_audit.sh status.sh uninstall.sh
ssh -p 22 -o ConnectTimeout=10 wodeluyouqi@192.168.50.1 "
set -eu
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
if ps w | grep -E '[p]ipeline_auto.py|[p]ipeline_trial.py|[d]aily_worker.py|[h]ome_probe.py' >/dev/null; then
  echo 'Probe still running; wait for its batch before updating.' >&2
  exit 3
fi
iptv_stage=/opt/tmp/iptv-daily-$iptv_ref
mkdir -p \"\$iptv_stage\"
tar -xzf - -C \"\$iptv_stage\"
IPTV_HOME_REF=$iptv_ref IPTV_HOME_STAGEDIR=\"\$iptv_stage\" IPTV_HOME_SKIP_INITIAL_RUN=1 /bin/sh \"\$iptv_stage/install.sh\"
/opt/bin/python3 -I -X utf8 /opt/share/iptv-home-probe/daily_readiness.py
" < "$iptv_stage/bundle.tar.gz"
