#!/bin/sh
# Build on GitHub/Linux. Never on the router.
set -eu
out=${1:-/tmp/iptv-native-build}
mkdir -p "$out/include/curl"
cp /usr/include/x86_64-linux-gnu/curl/*.h "$out/include/curl/"
aarch64-linux-gnu-gcc -Os -s -Wall -Wextra -Werror -Wno-deprecated-declarations \
  -I "$out/include" -Wl,--dynamic-linker=/opt/lib/ld-linux-aarch64.so.1 \
  -Wl,-rpath,/opt/lib -o "$out/iptv-native" router/ac86u/native/iptv_native.c -ldl -lm
# Entware compatibility: do not accidentally require the build runner's libc.
aarch64-linux-gnu-readelf -V "$out/iptv-native" > "$out/abi.txt"
python3 - "$out" <<'PY'
import hashlib, json, pathlib, re, sys
out = pathlib.Path(sys.argv[1])
versions = [tuple(map(int, x.split('.'))) for x in re.findall(r'GLIBC_([\d.]+)', (out/'abi.txt').read_text())]
assert versions and max(versions) <= (2, 27), versions
binary = out/'iptv-native'
assert binary.stat().st_size < 64 * 1024
manifest = {'architecture':'aarch64', 'maximum_glibc':'2.27',
    'source_sha256':hashlib.sha256(pathlib.Path('router/ac86u/native/iptv_native.c').read_bytes()).hexdigest(),
    'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(), 'bytes':binary.stat().st_size}
(out/'build.json').write_text(json.dumps(manifest, indent=2)+'\n')
print(json.dumps(manifest))
PY
