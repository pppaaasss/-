#!/bin/bash
# Runs inside the existing protected publication workflow, under its shared lock.
set -euo pipefail
iptv_repo=$(pwd)
iptv_reports=${1:?Report worktree required}
iptv_control=${2:?Control worktree required}
if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open("config/home-thin.json"))["enabled"] is True else 1)'; then
  echo 'Home thin controller disabled'
  exit 0
fi
if git ls-remote --exit-code --heads origin home-control >/dev/null; then
  git fetch --no-tags origin refs/heads/home-control:refs/remotes/origin/home-control
  git worktree add --detach "$iptv_control" refs/remotes/origin/home-control
else
  iptv_remote_status=$?
  test "$iptv_remote_status" -eq 2 || exit "$iptv_remote_status"
  git worktree add --detach "$iptv_control" HEAD
  git -C "$iptv_control" switch --orphan home-control
fi
python3 scripts/home_thin_control.py --repo-root "$iptv_repo" --reports "$iptv_reports" --control "$iptv_control"
for iptv_target in "$iptv_reports" "$iptv_control"; do
  git -C "$iptv_target" config user.name 'github-actions[bot]'
  git -C "$iptv_target" config user.email '41898282+github-actions[bot]@users.noreply.github.com'
done
git -C "$iptv_reports" add -- inbox
if test -d "$iptv_reports/observations"; then
  git -C "$iptv_reports" add -- observations
fi
if test -d "$iptv_reports/native-rejections"; then
  git -C "$iptv_reports" add -- native-rejections
fi
if ! git -C "$iptv_reports" diff --cached --quiet; then
  git -C "$iptv_reports" commit -m 'Aggregate bounded household observations'
  iptv_pushed=false
  for iptv_attempt in 1 2 3; do
    if git -C "$iptv_reports" push origin HEAD:refs/heads/home-reports; then
      iptv_pushed=true
      break
    fi
    # Router uploads append unique files. Rebase preserves concurrent uploads.
    git -C "$iptv_reports" fetch origin home-reports
    git -C "$iptv_reports" rebase FETCH_HEAD
  done
  test "$iptv_pushed" = true
fi
# Reports first, then state/tasks. If interrupted, the next pass regenerates
# the same report from immutable observations before advancing control state.
git -C "$iptv_control" add -- state.json status.json tasks
if test -f "$iptv_control/delivery-receipt.json"; then
  git -C "$iptv_control" add -- delivery-receipt.json
fi
if ! git -C "$iptv_control" diff --cached --quiet; then
  git -C "$iptv_control" commit -m 'Update household task queue and evidence history'
  git -C "$iptv_control" push origin HEAD:refs/heads/home-control
fi
