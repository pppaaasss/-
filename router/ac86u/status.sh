#!/bin/sh
set -eu

CONFIG="/opt/etc/iptv-home-probe.json"
DATA="/opt/var/lib/iptv-home-probe"
LOG="/opt/var/log/iptv-home-probe.log"

/opt/bin/python3 - "$CONFIG" "$DATA" <<'PY'
import json, sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
data = Path(sys.argv[2])
print(f"probe_id:       {config.get('probe_id')}")
print(f"upload:         {'enabled' if config.get('upload_enabled') else 'not paired'}")
print(f"production use: {'active' if config.get('actionable') else 'shadow only'}")
for filename in ("latest.json", "state.json", "upload-state.json"):
    path = data / filename
    if not path.exists():
        print(f"{filename}: missing")
        continue
    value = json.loads(path.read_text(encoding="utf-8"))
    if filename == "latest.json":
        summary = value.get("summary") or {}
        print(
            f"latest:         {value.get('generated_utc')} run={value.get('run_kind', '-')} "
            f"GOOD={summary.get('good', 0)} BAD={summary.get('bad', 0)} "
            f"UNKNOWN={summary.get('unknown', 0)} BACKUPS={summary.get('qualified_backups', 0)} "
            f"REPLACE={summary.get('replacements', 0)} QUEUE={summary.get('candidate_queue_remaining', 0)} "
            f"CIRCUIT={int(bool(summary.get('circuit_breaker_open')))}"
        )
    elif filename == "upload-state.json":
        print(
            f"uploads:        {value.get('successful_uploads', 0)} "
            f"first={value.get('first_upload_utc', '-')} last={value.get('last_upload_utc', '-')}"
        )
PY

echo "cron:"
cru l 2>/dev/null | grep -E 'IPTVHome(Primary|Recheck)' || echo "  missing"
echo "last log lines:"
tail -n 12 "$LOG" 2>/dev/null || true
