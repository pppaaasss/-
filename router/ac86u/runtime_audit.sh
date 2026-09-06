#!/bin/sh
# Collect runtime evidence without depending on Python or modifying packages.
umask 077
iptv_root=${1:-/opt/var/lib/iptv-home-probe/pipeline-trial}
iptv_mode=${2:-check}
iptv_log=${3:-/tmp/iptv-pipeline-trial.log}
iptv_audit="$iptv_root/runtime-diagnostics"
mkdir -p "$iptv_audit" || exit 1
mkdir "$iptv_audit/collect.lock" 2>/dev/null || exit 0
trap 'rmdir "$iptv_audit/collect.lock" 2>/dev/null' EXIT
[ ! -f "$iptv_audit/latest.txt" ] || cp "$iptv_audit/latest.txt" "$iptv_audit/previous.txt"
[ ! -f "$iptv_audit/current.sha256" ] || cp "$iptv_audit/current.sha256" "$iptv_audit/previous.sha256"
# A timeout kills only the diagnostic subprocess that this script created.
iptv_probe() {
    /opt/bin/python3 "$@" -c 'import json, ssl, pathlib; print("PYTHON_OK")' &
    iptv_probe_pid=$!
    (sleep 12; kill -KILL "$iptv_probe_pid" 2>/dev/null) &
    iptv_timer_pid=$!
    wait "$iptv_probe_pid"
    iptv_result=$?
    kill "$iptv_timer_pid" 2>/dev/null
    wait "$iptv_timer_pid" 2>/dev/null
    echo "PROBE_EXIT: $iptv_result"
    return "$iptv_result"
}
{
    date
    echo "MODE: $iptv_mode"
    uptime
    echo "MEMORY:"
    grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Slab|SReclaimable|SUnreclaim|PageTables|Committed_AS|CommitLimit):' /proc/meminfo
    echo "MOUNTS:"
    awk '$3 == "ext4" || $2 == "/opt" || $2 == "/tmp/opt" {print}' /proc/mounts
    echo "SPACE:"
    df -k /opt "$iptv_root"
    echo "PACKAGES:"
    /opt/bin/opkg list-installed | grep -E '^(python3|libpython3|libc |libgcc |libopenssl)'
    echo "RUNTIME_LINKS:"
    ls -l /opt/bin/python3* /opt/lib/libpython3* /opt /tmp/opt
    echo "LIVE_PYTHON_MEMORY_AND_LIBRARIES:"
    for iptv_proc in /proc/[0-9]*; do
        read -r iptv_comm < "$iptv_proc/comm" 2>/dev/null || continue
        case "$iptv_comm" in python*)
            echo "$iptv_proc"
            grep -E '^(Name|State|VmRSS|VmSize|VmSwap):' "$iptv_proc/status"
            grep 'libpython' "$iptv_proc/maps"
            ;;
        esac
    done
    echo "RUNTIME_HASHES:"
    : > "$iptv_audit/current.sha256"
    iptv_hash_ok=1
    for iptv_file in /opt/bin/python3 /opt/lib/libpython3*.so*; do
        [ -f "$iptv_file" ] || continue
        sha256sum "$iptv_file" >> "$iptv_audit/current.sha256" || iptv_hash_ok=0
    done
    cat "$iptv_audit/current.sha256"
    if [ -f "$iptv_audit/baseline.sha256" ]; then
        echo "COMPARE_HEALTHY_BASELINE:"
        sha256sum -c "$iptv_audit/baseline.sha256"
    else
        echo "NO_HEALTHY_BASELINE_YET"
    fi
    echo "RECENT_PIPELINE_LOG:"
    tail -n 80 "$iptv_log"
    echo "KERNEL_ERRORS:"
    dmesg | grep -Ei 'out of memory|killed process|I/O error|EXT4-fs error|segfault|usb.*(reset|disconnect)|voltage' | tail -n 40
    echo "PYTHON_ENV_PRESENT:"
    for iptv_name in LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; do
        printenv "$iptv_name" >/dev/null 2>&1 && echo "$iptv_name is set"
    done
    unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
    echo "CLEAN_NORMAL_START:"
    iptv_probe
    iptv_normal=$?
    echo "ISOLATED_START:"
    iptv_probe -I -S
    iptv_isolated=$?
    if [ "$iptv_normal" -eq 0 ] && [ "$iptv_isolated" -eq 0 ] && [ "$iptv_hash_ok" -eq 1 ]; then
        if [ ! -f "$iptv_audit/baseline.sha256" ]; then
            cp "$iptv_audit/current.sha256" "$iptv_audit/baseline.sha256"
            cp "$iptv_audit/latest.txt" "$iptv_audit/baseline-context.txt"
            echo "HEALTHY_BASELINE_SAVED"
        fi
        echo "RUNTIME_CHECK_OK"
    else
        echo "RUNTIME_CHECK_FAILED"
    fi
} > "$iptv_audit/latest.txt" 2>&1
# Retain the first failure even if a later successful check overwrites latest.
if [ "$iptv_mode" = failure ] && [ ! -f "$iptv_audit/first-failure.txt" ]; then
    cp "$iptv_audit/latest.txt" "$iptv_audit/first-failure.txt"
fi
cat "$iptv_audit/latest.txt"
