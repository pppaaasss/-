#!/usr/bin/env python3
"""PHONE ONLY: archive frozen legacy IPTV files before removing their router copy.

The router runs tar and its existing native lock helper. One SSH connection holds
all worker locks across backup, phone validation/fsync, and cleanup. No ACK means
no deletion. Existing native runtime, startup block and shared packages stay put.
"""
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import secrets
import select
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time

INSTALL_REF = 'ae703187e2219dcda08cbc4105c78acd3f3fa912'
ROOTS = (
    'opt/var/lib/iptv-home-probe',
    'opt/share/iptv-home-probe',
    'opt/var/log/iptv-home-probe.log',
    'opt/var/log/iptv-home-probe.log.1',
    'opt/etc/iptv-home-probe.json',
    'opt/etc/iptv-home-probe',
)
REQUIRED = (
    ROOTS[0]+'/thin-mode.locked',
    ROOTS[0]+'/before-native-v1/staged',
)
MAX_BACKUP = 100 * 1024 * 1024


def remote_script(marker):
    """All destructive paths are literal allowlisted legacy project paths."""
    return r'''set -eu
umask 077
old=/opt/var/lib/iptv-home-probe
data=/opt/var/lib/iptv-home-native
fail() { echo "CLEANUP_STOPPED:$1" >&2; exit 2; }
guard() {
    [ -d "$old" ] && [ ! -L "$old" ] || fail old_directory
    [ -f "$old/thin-mode.locked" ] || fail legacy_not_frozen
    [ -f "$old/before-native-v1/staged" ] || fail native_not_staged
    [ ! -e "$data/ENABLED" ] || fail sampler_enabled
    [ ! -e "$data/PAUSED" ] || fail sampler_needs_attention
    [ ! -e "$data/outbox.native" ] || fail upload_pending
    [ ! -e /opt/var/run/iptv-home-probe.lock ] || fail old_worker_active
    grep -q 'UPLOADED$' "$data/status.txt" || fail sample_not_uploaded
    grep -q '^COMPLETED[[:space:]]' "$data/resources.txt" || fail sample_not_completed
    jobs=$(cru l) || fail cron_unreadable
    count=$(printf '%s\n' "$jobs" | awk '/#IPTVHome(Probe|Primary|Recheck|Peak|Resume|Thin)#/{n++} END{print n+0}')
    [ "$count" = 0 ] || fail old_cron_present
    iptables -t nat -S merlinclash >/dev/null || fail vpn_chain_missing
}
guard
vpn=$(iptables -t nat -S merlinclash)
startup=$(/opt/share/iptv-home-native/iptv-native sha256 /jffs/scripts/services-start)
cd /
set --
for path in ''' + ' '.join(ROOTS) + r'''; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        [ ! -L "$path" ] || fail legacy_root_is_symlink
        set -- "$@" "$path"
    fi
done
before=$(du -sk "$@" | awk '{n+=$1} END{print n+0}')
# Plain tar streams directly to the phone: no second router copy or compressor.
tar -cf - "$@"
printf '%s\n' ''' + shlex.quote(marker) + r'''
IFS= read -r acknowledgement
[ "$acknowledgement" = ''' + shlex.quote('VERIFIED '+marker) + r''' ] || fail backup_not_verified
guard
# Retain lock inodes and the small migration markers required by native activate.
for path in "$old"/* "$old"/.[!.]* "$old"/..?*; do
    [ -e "$path" ] || [ -L "$path" ] || continue
    case "$path" in
        "$old/before-native-v1"|"$old/thin-mode.locked"|"$old/"*.lock|"$old/background-upgrade.locked") continue;;
    esac
    rm -rf "$path"
done
rm -rf /opt/share/iptv-home-probe /opt/etc/iptv-home-probe
rm -f /opt/etc/iptv-home-probe.json /opt/var/log/iptv-home-probe.log /opt/var/log/iptv-home-probe.log.1
guard
[ "$vpn" = "$(iptables -t nat -S merlinclash)" ] || fail vpn_changed
[ "$startup" = "$(/opt/share/iptv-home-native/iptv-native sha256 /jffs/scripts/services-start)" ] || fail startup_changed
after=$(du -sk "$old" | awk '{print $1}')
printf 'CLEANUP_OK freed_KiB=%s\n' "$((before-after))"
printf '新测速：关闭；旧任务：0；VPN规则链：未改动\n'
'''


def remote_command(marker):
    return ('unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; '
            'export PATH=/opt/bin:/opt/sbin:/usr/sbin:/usr/bin:/sbin:/bin; '
            'exec /opt/share/iptv-home-native/iptv-native lock '
            '/opt/var/lib/iptv-home-native /opt/var/lib/iptv-home-probe '
            '/bin/sh -c '+shlex.quote(remote_script(marker)))


def receive_backup(stream, destination, marker):
    boundary = (marker+'\n').encode()
    tail = b''
    total = 0
    deadline = time.monotonic()+180
    with destination.open('xb') as output:
        while True:
            left = deadline-time.monotonic()
            if left <= 0 or not select.select([stream], [], [], min(30, left))[0]:
                raise RuntimeError('备份传输超时，未发送清理指令')
            block = stream.read1(65536)
            if not block:
                raise RuntimeError('备份未传完，未发送清理指令')
            tail += block
            where = tail.find(boundary)
            if where >= 0:
                if where+len(boundary) != len(tail):
                    raise RuntimeError('备份结束标记异常')
                output.write(tail[:where])
                total += where
                break
            keep = len(boundary)-1
            output.write(tail[:-keep])
            total += max(0, len(tail)-keep)
            tail = tail[-keep:]
            if total > MAX_BACKUP:
                raise RuntimeError('备份超过 100 MiB，已停止清理')
        if total > MAX_BACKUP or total < 1024 or total % 512:
            raise RuntimeError('备份大小异常，已停止清理')
        output.flush()
        os.fsync(output.fileno())


def verify_backup(path):
    """Read every member, never extract archive paths or interpret old JSON."""
    regular = set()
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            name = member.name.rstrip('/')
            if (PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                    or not any(name == root or name.startswith(root+'/') for root in ROOTS)):
                raise RuntimeError('备份包含非旧项目路径')
            if member.isfile():
                regular.add(name)
                with archive.extractfile(member) as source:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(65536, remaining))
                        if not block:
                            raise RuntimeError('备份文件被截断')
                        remaining -= len(block)
    if not set(REQUIRED) <= regular:
        raise RuntimeError('备份缺少迁移标记')
    with path.open('rb') as source:
        source.seek(-1024, os.SEEK_END)
        if source.read() != b'\0'*1024:
            raise RuntimeError('备份结尾不完整')
        source.seek(0)
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    return digest


def cleanup(installer, folder):
    marker = 'IPTV_ARCHIVE_END_'+secrets.token_hex(24)
    partial, backup = folder/'legacy.tar.part', folder/'legacy.tar'
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(['ssh', *installer.SSH_OPTIONS, installer.HOST,
                                    remote_command(marker)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            receive_backup(process.stdout, partial, marker)
            digest = verify_backup(partial)
            partial.rename(backup)
            with (folder/'SHA256SUMS').open('x') as manifest:
                manifest.write(digest+'  legacy.tar\n')
                manifest.flush()
                os.fsync(manifest.fileno())
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            print('手机备份已校验，正在清理旧文件。', flush=True)
            output, _ = process.communicate(('VERIFIED '+marker+'\n').encode(), timeout=60)
            if process.returncode:
                errors.seek(0)
                raise installer.remote_error(subprocess.CompletedProcess([], process.returncode,
                                                                         stderr=errors.read()))
            if not output.startswith(b'CLEANUP_OK '):
                raise RuntimeError('未收到清理完成回执；备份已保留')
            print(output.decode().strip())
            print('备份：'+str(backup))
        except BaseException:
            # EOF aborts the remote read before deletion when no ACK was sent.
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            errors.seek(0)
            detail = errors.read().decode(errors='replace').splitlines()
            for line in detail[-3:]:
                print(line, file=sys.stderr)
            if backup.exists():
                print('保留备份：'+str(backup), file=sys.stderr)
            raise
        finally:
            process.stdout.close()


def main():
    os.umask(0o077)
    installed = Path.home()/('iptv-native-'+INSTALL_REF)/'native_from_termux.py'
    spec = importlib.util.spec_from_file_location('iptv_installer', installed)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.cloud_ready(Path.home()/'iptv-thin.token')
    folder = Path(tempfile.mkdtemp(prefix='iptv-legacy-backup-', dir=Path.home()))
    with installer.ssh_session():
        cleanup(installer, folder)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print('清理未完成：'+str(error), file=sys.stderr)
        sys.exit(1)
