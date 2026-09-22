#!/bin/sh
# Fixed data-only task protocol. No eval/source, Python, FFprobe, Git or history.
set -eu
umask 077
base=${IPTV_NATIVE_BASE:-/opt/share/iptv-home-native}
data=${IPTV_NATIVE_DATA:-/opt/var/lib/iptv-home-native}
native=$base/iptv-native
api=https://api.github.com/repos/pppaaasss/-
uploads=https://uploads.github.com/repos/pppaaasss/-
auth=$data/github.curl
tab=$(printf '\t')
mkdir -p "$data/work"
work=$data/work
read -r probe dns < "$data/identity"
case "$probe" in *[!a-z0-9-]*|'') exit 2;; esac
case "$dns" in *[!0-9.]*|'') exit 2;; esac
mark=$((0x49600000 + ($$ % 65536)))

status() { printf '%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" > "$data/status.txt"; }
api_get() {
  curl --config "$auth" --silent --show-error --fail --proto '=https' \
    --connect-timeout 5 --max-time 15 --max-filesize 98304 \
    --header 'Accept: application/vnd.github.raw+json' "$api/$1" -o "$2"
}
notify() {
  curl --config "$auth" --silent --show-error --fail --proto '=https' \
    --connect-timeout 5 --max-time 10 --header 'Content-Type: application/json' \
    --data '{"event_type":"home-observations-ready"}' "$api/dispatches" -o /dev/null || true
}
upload() {
  # An interrupted batch keeps only atomic, completed per-route checkpoints.
  if ! tail -n 1 "$data/outbox.native" | grep -q '^END'; then
    # The last durable row carries its own time; no separate timestamp race.
    finish=$(tail -n 1 "$data/outbox.native" | cut -f5)
    cp "$data/outbox.native" "$work/closed"
    printf 'END\t%s\tinterrupted_batch\n' "$finish" >> "$work/closed"
    "$native" commit "$work/closed" "$data/outbox.native"
  fi
  read -r batch release < "$data/outbox-id"
  case "$batch" in *[!0-9a-f]*|'') exit 2;; esac
  [ "${#batch}" -eq 64 ] || exit 2
  case "$release" in *[!0-9]*|'') exit 2;; esac
  sum=$("$native" sha256 "$data/outbox.native")
  name=$batch-$sum.native
  # Stream the bounded file; --data-binary would read the entire file into RAM.
  http=$(curl --config "$auth" --silent --show-error --proto '=https' \
    --connect-timeout 5 --max-time 30 --request POST --upload-file "$data/outbox.native" \
    --header 'Content-Type: application/octet-stream' --output "$work/upload-reply" \
    --write-out '%{http_code}' "$uploads/releases/$release/assets?name=$name") || {
      status WAITING_UPLOAD; return 1;
    }
  if [ "$http" = 422 ]; then
    # Lost replies may leave the same uniquely named, checksum-bound asset.
    api_get "releases/$release/assets?per_page=100" "$work/assets" || return 1
    grep -Eq '"name"[[:space:]]*:[[:space:]]*"'"$name"'"' "$work/assets" || return 1
  elif [ "$http" != 201 ]; then
    status "UPLOAD_HTTP_$http"; return 1
  fi
  printf '%s\n' "$batch" > "$work/ack"
  "$native" commit "$work/ack" "$data/last-uploaded"
  rm -f "$data/outbox.native" "$data/checkpoint-time" "$data/outbox-id"
  rm -f "$work"/*
  status UPLOADED
  notify
}

if [ -f "$data/outbox.native" ]; then upload; exit 0; fi
api_get "contents/tasks/$probe.native?ref=home-control" "$work/task" || { status WAITING_CLOUD; exit 0; }
IFS="$tab" read -r wire task_probe batch cycle created expires seconds release < "$work/task"
if [ "$wire" = IDLE ]; then status "$task_probe"; exit 0; fi
[ "$wire" = IPTV_NATIVE_V1 ] && [ "$task_probe" = "$probe" ] || exit 2
case "$batch$cycle" in *[!0-9a-f]*|'') exit 2;; esac
[ "${#batch}" -eq 64 ] && [ "${#cycle}" -eq 64 ] || exit 2
for numeric in "$created" "$expires" "$seconds" "$release"; do
  case "$numeric" in *[!0-9]*|'') exit 2;; esac
done
[ "$seconds" -le 240 ] && [ "$seconds" -gt 0 ] || exit 2
now=$(date +%s)
if [ "$now" -lt "$created" ] || [ "$now" -ge "$expires" ]; then status WAITING_WINDOW; exit 0; fi
[ "$(cat "$data/last-uploaded" 2>/dev/null || true)" != "$batch" ] || { status WAITING_CLOUD_ACK; exit 0; }
sed '$d' "$work/task" > "$work/task-body"
check=$("$native" sha256 "$work/task-body")
IFS="$tab" read -r check_tag expected <<EOF
$(tail -n 1 "$work/task")
EOF
[ "$check_tag" = SHA256 ] && [ "$check" = "$expected" ] || exit 2
# Explicitly refuse excess tasks before the first download.
count=$(grep -c '^T' "$work/task-body")
[ "$count" -ge 1 ] && [ "$count" -le 4 ] || exit 2
printf '%s %s\n' "$batch" "$release" > "$work/outbox-id"
"$native" commit "$work/outbox-id" "$data/outbox-id"
printf 'IPTV_NATIVE_V1\t%s\t%s\t%s\t%s\n' "$probe" "$batch" "$cycle" "$now" > "$work/new-outbox"
"$native" commit "$work/new-outbox" "$data/outbox.native"

cleanup_route() {
  "$native" route-clean "$data" || true
}
trap cleanup_route EXIT
trap 'exit 75' HUP INT TERM
iptables -t nat -S merlinclash >/dev/null
# Persist intent before insertion, so a killed worker can be cleaned up later.
printf '%s\n' "$mark" > "$work/route-mark"
"$native" commit "$work/route-mark" "$data/route-mark"
iptables -t nat -I OUTPUT 1 -p tcp -m mark --mark "$mark/0xffffffff" -j merlinclash
# Only the native sampler sets this mark. API uploads retain their normal path.
status RUNNING
started=$now
completion=completed
sed '1d' "$work/task-body" > "$work/routes"
while IFS="$tab" read -r row id encoded limit; do
  [ "$row" = T ] || exit 2
  case "$id" in *[!0-9a-f]*|'') exit 2;; esac
  [ "${#id}" -eq 64 ] || exit 2
  case "$limit" in *[!0-9]*|'') exit 2;; esac
  [ "$limit" -ge 65536 ] && [ "$limit" -le 6291456 ] || exit 2
  now=$(date +%s)
  [ "$((now-started))" -lt "$((seconds-50))" ] && [ "$now" -lt "$((expires-50))" ] || { completion=interrupted_batch; break; }
  url=$(printf '%s' "$encoded" | base64 -d)
  begin=$now
  outcome=unsupported
  rm -f "$work/first.bin" "$work/sample1" "$work/sample2" "$work/manifest-metric" "$work/selection"
  extra=0
  current=$url
  for depth in 1 2 3; do
    "$native" get "$current" 98304 98304 "$work/playlist" "$work/manifest" "$mark" "$dns" 53
    IFS="$tab" read -r rc http size elapsed total complete final < "$work/manifest"
    extra=$((extra+size))
    printf '%s\t0\n' "$(cat "$work/manifest")" > "$work/manifest-metric"
    if [ "$rc" -ne 0 ] || [ "$http" -lt 200 ] || [ "$http" -ge 300 ]; then outcome=transfer_error; break; fi
    "$native" hls "$work/playlist" "$final" > "$work/selection" || break
    IFS="$tab" read -r kind next < "$work/selection"
    case "$kind" in
      MASTER) current=$next ;;
      DIRECT) printf 'SEGMENT\t0\t%s\nSEGMENT\t0\t%s\n' "$url" "$url" > "$work/selection"; break ;;
      *) break ;;
    esac
  done
  if grep -q '^SEGMENT' "$work/selection" 2>/dev/null; then
    sample=0
    while IFS="$tab" read -r kind duration segment; do
      [ "$kind" = SEGMENT ] || break
      sample=$((sample+1)); [ "$sample" -le 2 ] || break
      "$native" get "$segment" "$limit" 131072 "$work/prefix" "$work/metric" "$mark" "$dns" 53
      if [ "$sample" -eq 1 ]; then cp "$work/prefix" "$work/first.bin"; fi
      printf '%s\t%s\n' "$(cat "$work/metric")" "$duration" > "$work/sample$sample"
      IFS="$tab" read -r rc http size rest < "$work/metric"
      if [ "$rc" -ne 0 ] || [ "$http" -lt 200 ] || [ "$http" -ge 300 ]; then outcome=transfer_error; break; fi
      [ "$sample" -ne 2 ] || outcome=measured
    done < "$work/selection"
  fi
  finish=$(date +%s)
  # An expired or guardian-stopped route never becomes channel-failure evidence.
  [ "$finish" -lt "$expires" ] || { completion=interrupted_batch; break; }
  {
    printf 'R\t%s\t%s\t%s\t%s\t%s\t' "$id" "$encoded" "$begin" "$finish" "$extra"
    base64 "$work/manifest-metric" | tr -d '\n'
    printf '\t%s\t' "$outcome"
    if [ -s "$work/first.bin" ]; then base64 "$work/first.bin" | tr -d '\n'; else printf '-'; fi
    for file in "$work/sample1" "$work/sample2"; do
      printf '\t'; if [ -f "$file" ]; then base64 "$file" | tr -d '\n'; else printf '-'; fi
    done
    printf '\n'
  } > "$work/row"
  cat "$data/outbox.native" "$work/row" > "$work/checkpoint"
  "$native" commit "$work/checkpoint" "$data/outbox.native"
  # Give the VPN/TV a gap between independent channels.
  sleep 2
done < "$work/routes"
cleanup_route
trap - EXIT HUP INT TERM
finish=$(tail -n 1 "$data/outbox.native" | cut -f5)
cp "$data/outbox.native" "$work/closed"
printf 'END\t%s\t%s\n' "$finish" "$completion" >> "$work/closed"
"$native" commit "$work/closed" "$data/outbox.native"
upload
