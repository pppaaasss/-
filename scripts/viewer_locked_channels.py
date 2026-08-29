#!/usr/bin/env python3
"""Keep viewer-confirmed channels present without freezing a dead URL forever.

The preferred URL is used only when a locked channel is missing.  If the
channel already exists, its current metadata and route are preserved so the
confirmed-dead failover may still replace a failed route.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path("config/viewer-locked-channels.json")


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else ""


def group_name(extinf: str) -> str:
    match = re.search(r'group-title="([^"]*)"', extinf, re.I)
    return match.group(1).strip() if match else ""


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise RuntimeError(f"invalid viewer lock config: {path}")
    return data


def target_rows(config: dict, playlist_name: str) -> list[dict]:
    rows = []
    for row in config.get("channels") or []:
        if playlist_name not in (row.get("playlists") or []):
            continue
        required = ("name", "group", "logo", "preferred_url")
        if not all(str(row.get(key) or "").strip() for key in required):
            raise RuntimeError(f"invalid locked channel for {playlist_name}: {row}")
        rows.append(row)
    return rows


def render_extinf(row: dict) -> str:
    return (
        f'#EXTINF:-1 tvg-logo="{row["logo"]}" '
        f'group-title="{row["group"]}",{row["name"]}'
    )


def ensure_path(path: Path, config: dict) -> list[str]:
    if not path.exists():
        return []
    original = path.read_text(encoding="utf-8", errors="ignore")
    lines = original.splitlines()
    existing = {
        visible_name(line)
        for line in lines
        if line.startswith("#EXTINF") and visible_name(line)
    }
    missing = [row for row in target_rows(config, path.name) if row["name"] not in existing]
    if not missing:
        return []

    # Keep the restored tiles beside the existing entries in their APTV group.
    insert_at = len(lines)
    wanted_groups = {str(row["group"]) for row in missing}
    for index, line in enumerate(lines):
        if line.startswith("#EXTINF") and group_name(line) in wanted_groups:
            insert_at = min(index + 2, len(lines))

    additions = []
    for row in missing:
        additions.extend((render_extinf(row), str(row["preferred_url"])))
    lines[insert_at:insert_at] = additions
    rendered = "\n".join(lines).rstrip() + "\n"
    path.write_text(rendered, encoding="utf-8", newline="\n")
    return [str(row["name"]) for row in missing]


def ensure_locked_channels(
    root: Path = ROOT,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, list[str]]:
    config = load_config(root / config_path)
    playlist_names = sorted({name for row in config["channels"] for name in row["playlists"]})
    return {name: ensure_path(root / name, config) for name in playlist_names}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    changed = ensure_locked_channels(Path(args.repo_root), Path(args.config))
    for name, added in changed.items():
        print(f"{name}: added={','.join(added) if added else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
