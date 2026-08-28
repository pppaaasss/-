#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

PAIR_RE = re.compile(r"([A-Z_]+)=([^\s]+)")


def read_progress(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    return {k: v for k, v in PAIR_RE.findall(text)}


def process_alive(pattern: str) -> bool:
    proc = subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="pppaaasss/-")
    ap.add_argument("--issue", type=int, default=2)
    ap.add_argument("--progress", default="/var/lib/iptv-hk-probe/all-url-audit/progress.txt")
    ap.add_argument("--token-file", default="/etc/iptv-hk-probe.github-token")
    args = ap.parse_args()

    token_path = Path(args.token_file)
    if not token_path.exists():
        print("GitHub token file missing", flush=True)
        return 2
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        print("GitHub token file empty", flush=True)
        return 2

    p = read_progress(Path(args.progress))
    total = p.get("TOTAL", "?")
    done = p.get("DONE", "?")
    remaining = p.get("REMAINING", "?")
    good = p.get("GOOD", "?")
    degraded = p.get("DEGRADED", "?")
    unknown = p.get("UNKNOWN", "?")
    percent = p.get("PROGRESS", "?")

    scanner = process_alive(r"scripts/hk_audit_all_urls\.py")
    finalizer = process_alive(r"scripts/hk_finalize_after_audit\.sh")
    complete = False
    try:
        complete = int(done) >= int(total) > 0
    except Exception:
        pass

    if complete:
        state = "✅ 全量扫描完成"
    elif scanner:
        state = "🟢 全量扫描运行中"
    else:
        state = "⚠️ 扫描进程未运行"

    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    body = f"""# HK IPTV Probe Live Status

**状态：{state}**  
**最后回传：{now}**

| 指标 | 当前值 |
|---|---:|
| TOTAL | {total} |
| DONE | {done} |
| REMAINING | {remaining} |
| GOOD | {good} |
| DEGRADED | {degraded} |
| UNKNOWN | {unknown} |
| PROGRESS | {percent} |

- 扫描进程：{'运行中' if scanner else '未运行'}
- 扫完收尸进程：{'运行中/等待中' if finalizer else '未运行'}
- 回传周期：10 分钟
- 不回传服务器 IP、SSH 信息或 GitHub Token。

> 此 Issue 由香港 IPTV 探针自动覆盖更新。
"""

    payload = json.dumps({"body": body}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{args.repo}/issues/{args.issue}",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hk-iptv-probe-status/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                print(f"GitHub issue update failed HTTP {resp.status}", flush=True)
                return 3
    except Exception as exc:
        print(f"GitHub issue update failed: {type(exc).__name__}: {exc}", flush=True)
        return 3

    print(f"HK_STATUS_REPORTED done={done} progress={percent}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
