#!/bin/sh
# Download on the phone, transfer one complete version, detach on the router.
set -eu
iptv_ref=${1:?Usage: trial_from_termux.sh COMMIT_SHA}
case "$iptv_ref" in *[!0-9a-f]*|'') exit 2 ;; esac
[ "${#iptv_ref}" -eq 40 ] || exit 2
iptv_stage=$(mktemp -d)
trap 'rm -rf "$iptv_stage"' EXIT HUP INT TERM
iptv_folder="iptv-pipeline-$iptv_ref"
mkdir "$iptv_stage/$iptv_folder"
iptv_base="https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref"
for iptv_name in home_probe.py home_resources.py home_transport.py home_contract.py home_decision.py pipeline_trial.py; do
    curl -4 -fSL --retry 2 --connect-timeout 10 --max-time 60 \
        "$iptv_base/router/ac86u/$iptv_name" -o "$iptv_stage/$iptv_folder/$iptv_name"
done
curl -4 -fSL --retry 2 --connect-timeout 10 --max-time 120 \
    "$iptv_base/harvest/home-trial-candidates.json.gz" \
    -o "$iptv_stage/$iptv_folder/home-trial-candidates.json.gz"
cat > "$iptv_stage/$iptv_folder/start.py" <<'PY'
from pathlib import Path
import subprocess
import sys
base = Path(__file__).resolve().parent
with open('/tmp/iptv-pipeline-trial.log', 'a') as log:
    process = subprocess.Popen([sys.executable, '-u', str(base / 'pipeline_trial.py')],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
print('PIPELINE_PID:', process.pid)
PY
tar -C "$iptv_stage" -cf "$iptv_stage/bundle.tar" "$iptv_folder"
ssh -p 22 wodeluyouqi@192.168.50.1 "
set -e
unset LD_LIBRARY_PATH LD_PRELOAD
tar -xf - -C /tmp
/opt/bin/python3 /tmp/$iptv_folder/start.py
sleep 4
tail -n 8 /tmp/iptv-pipeline-trial.log
" < "$iptv_stage/bundle.tar"
