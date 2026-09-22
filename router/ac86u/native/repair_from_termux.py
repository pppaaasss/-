#!/usr/bin/env python3
"""Phone-only, commit-pinned repair of an existing native runtime."""
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile

HOST = 'wodeluyouqi@192.168.50.1'
BASE = '/opt/share/iptv-home-native'
DATA = '/opt/var/lib/iptv-home-native'
LEGACY = '/opt/var/lib/iptv-home-probe'
FILES = ('iptv-native', 'native_worker.sh', 'native_status.sh')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fetch(ref, name):
    url = f'https://raw.githubusercontent.com/pppaaasss/-/{ref}/router/ac86u/native/{name}'
    print('下载更新文件：'+name, flush=True)
    # Use the same Termux curl transport that fetched this entry successfully.
    # A file output is truncated by curl before a retry, so a TLS interruption
    # cannot concatenate a partial response with the successful retry.
    with tempfile.TemporaryDirectory(prefix='iptv-download-') as folder:
        target = Path(folder)/'payload'
        result = subprocess.run([
            'curl', '--fail', '--location', '--silent', '--show-error',
            '--proto', '=https', '--proto-redir', '=https',
            '--connect-timeout', '10', '--max-time', '30',
            '--retry', '2', '--retry-all-errors', '--retry-delay', '1',
            '--retry-max-time', '60', '--max-filesize', '131072',
            '--output', str(target), url], capture_output=True, timeout=100)
        if result.returncode:
            detail = result.stderr.decode('utf-8', errors='replace').strip()[-500:]
            raise ValueError(f'{name} 下载失败（curl {result.returncode}）：{detail}')
        if target.stat().st_size > 131072:
            raise ValueError('下载文件超出预期大小：'+name)
        return target.read_bytes()


def validate(bundle, source, manifest):
    binary = bundle['iptv-native']
    if (manifest.get('architecture') != 'aarch64' or
            manifest.get('binary_sha256') != sha(binary) or
            manifest.get('source_sha256') != sha(source) or
            manifest.get('bytes') != len(binary) or len(binary) >= 65536 or
            binary[:4] != b'\x7fELF' or int.from_bytes(binary[18:20], 'little') != 183):
        raise ValueError('程序与构建清单不匹配，停止更新')


def repair_script(ref, bundle):
    # Use only existing native hash/commit commands. No router interpreter or
    # package installation; the lock owner remains the old inode until exit.
    backup = DATA+'/before-route-retry-'+ref[:12]
    lines = ['#!/bin/sh', 'set -eu', 'umask 077', 'work=$1',
             'base='+BASE, 'data='+DATA, 'backup='+backup,
             'native="$work/iptv-native"',
             '[ "$(uname -m)" = aarch64 ]',
             '[ -f "$data/ENABLED" ]',
             'cru l | grep -q "#IPTVHomeNative#"',
             '"$native" check',
             'mkdir -p "$backup"',
             # Save the evidence before replacing scripts or clearing a pause.
             'for name in resources.txt status.txt; do',
             '  [ ! -f "$data/$name" ] || cp "$data/$name" "$backup/$name"',
             'done',
             'cru l > "$backup/crontab.txt"']
    for name in FILES:
        lines.extend([f'[ -f "$base/{name}" ]',
                      f'[ "$("$native" sha256 "$work/{name}")" = {sha(bundle[name])} ]',
                      f'[ -f "$backup/{name}" ] || cp -p "$base/{name}" "$backup/{name}"'])
    lines.extend([
        'install_file() {',
        '  target="$base/$2"; staged="$target.route-repair.$$"',
        '  (set -C; : > "$staged") || return 1',
        '  if cp "$1" "$staged" && chmod 755 "$staged" && "$native" commit "$staged" "$target"; then return 0; fi',
        '  rm -f "$staged"; return 1',
        '}',
        'restore() {',
        '  trap - EXIT HUP INT TERM',
        '  failed=0',
        '  for name in iptv-native native_worker.sh native_status.sh; do',
        '    install_file "$backup/$name" "$name" || failed=1',
        '  done',
        '  if [ "$failed" = 0 ]; then echo REPAIR_ROLLED_BACK >&2; else echo REPAIR_RESTORE_FAILED >&2; fi',
        '  exit 2',
        '}',
        'trap restore EXIT HUP INT TERM'])
    for name in FILES:
        lines.extend([f'install_file "$work/{name}" {name}',
                      f'[ "$("$native" sha256 "$base/{name}")" = {sha(bundle[name])} ]'])
    lines.extend([
        '"$base/iptv-native" check',
        'trap - EXIT HUP INT TERM',
        # This is explicit, one-time migration of the known old fault. Other
        # pause reasons or an uncertain firewall state stay paused.
        '"$base/iptv-native" recover-route-pause "$data"',
        'printf "ROUTE_REPAIR_OK: cron will resume within 30 seconds in an allowed window\\n"',
        'sh "$base/native_status.sh"'])
    return ('\n'.join(lines)+'\n').encode()


def prepare(ref):
    if not re.fullmatch(r'[0-9a-f]{40}', ref):
        raise ValueError('需要固定的40位提交编号')
    bundle = {name: fetch(ref, name) for name in FILES}
    source = fetch(ref, 'iptv_native.c')
    manifest = json.loads(fetch(ref, 'build.json'))
    validate(bundle, source, manifest)
    bundle['repair.sh'] = repair_script(ref, bundle)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for name, raw in bundle.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(raw), 0o700
            tar.addfile(item, io.BytesIO(raw))
    return archive.getvalue()


def main():
    if len(sys.argv) != 2:
        print('用法：python repair_from_termux.py <固定提交编号>', file=sys.stderr)
        return 2
    try:
        payload = prepare(sys.argv[1])
        print('文件已下载并校验。请输入路由器登录密码；输入时不显示字符。', flush=True)
        # A phone-generated unique name and exclusive mkdir work on the
        # router's minimal shell (which lacks mktemp). Keep installation alive across a
        # phone SSH disconnect; no token or password enters the payload.
        command = ('set -eu; umask 077; '
                   'unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; '
                   'export PATH=/opt/bin:/opt/sbin:/usr/sbin:/usr/bin:/sbin:/bin; '
                   'work=/opt/tmp/iptv-route-repair-'+secrets.token_hex(8)+'; mkdir "$work"; '
                   'trap \'rm -rf "$work"\' EXIT; '
                   'tar -xzf - -C "$work"; trap "" HUP; '
                   f'{BASE}/iptv-native lock {DATA} {LEGACY} '
                   '/bin/sh "$work/repair.sh" "$work"')
        result = subprocess.run(['ssh', '-p', '22', '-o', 'ConnectTimeout=10',
                                 '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                                 HOST, command], input=payload)
        if result.returncode:
            print('更新未确认完成，请保留上方错误输出。', file=sys.stderr)
        return result.returncode
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('更新停止：'+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
