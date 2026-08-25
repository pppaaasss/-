#!/usr/bin/env python3
"""Capture one frame from every channel in a playlist group for identity review."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class Entry:
    index: int
    name: str
    url: str


def parse_group(text: str, group: str) -> list[Entry]:
    entries: list[Entry] = []
    lines = text.splitlines()
    for pos, line in enumerate(lines[:-1]):
        if not line.startswith("#EXTINF") or f'group-title="{group}"' not in line:
            continue
        name = line.rsplit(",", 1)[-1].strip()
        url = lines[pos + 1].strip()
        if url and not url.startswith("#"):
            entries.append(Entry(len(entries) + 1, name, url))
    return entries


def safe_stem(entry: Entry) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff+_-]+", "_", entry.name).strip("_")
    return f"{entry.index:02d}_{slug or 'channel'}"


def run_capture(entry: Entry, output_dir: Path, timeout: int) -> dict[str, str | int]:
    frame = output_dir / f"{safe_stem(entry)}.jpg"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rw_timeout",
        "8000000",
        "-analyzeduration",
        "8000000",
        "-probesize",
        "8000000",
        "-user_agent",
        "Mozilla/5.0 (AppleTV; APTV playlist health-check/2.0)",
        "-i",
        entry.url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2",
        "-q:v",
        "3",
        "-y",
        str(frame),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        error = completed.stderr.strip().replace("\n", " | ")[-700:]
        ok = completed.returncode == 0 and frame.exists() and frame.stat().st_size > 1000
    except subprocess.TimeoutExpired as exc:
        ok = False
        error = f"timeout after {timeout}s"
        if exc.stderr:
            error += " | " + str(exc.stderr)[-500:]
    return {
        "index": entry.index,
        "name": entry.name,
        "url": entry.url,
        "status": "frame_ok" if ok else "frame_failed",
        "frame": str(frame) if ok else "",
        "error": error,
    }


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def make_sheets(rows: list[dict[str, str | int]], output_dir: Path) -> None:
    cols, page_size = 3, 12
    tile_w, frame_h, label_h = 640, 360, 60
    font = load_font(26)
    small_font = load_font(19)
    for page_index in range(0, len(rows), page_size):
        page_rows = rows[page_index : page_index + page_size]
        sheet_rows = (len(page_rows) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tile_w, sheet_rows * (frame_h + label_h)), "#202124")
        draw = ImageDraw.Draw(sheet)
        for slot, row in enumerate(page_rows):
            x = (slot % cols) * tile_w
            y = (slot // cols) * (frame_h + label_h)
            frame_path = str(row["frame"])
            if frame_path:
                with Image.open(frame_path) as source:
                    source = source.convert("RGB")
                    source.thumbnail((tile_w, frame_h))
                    px = x + (tile_w - source.width) // 2
                    py = y + (frame_h - source.height) // 2
                    sheet.paste(source, (px, py))
            else:
                draw.rectangle((x, y, x + tile_w, y + frame_h), fill="#34373c")
                draw.text((x + 18, y + 150), "抓图失败 / 无视频", fill="#ff8a80", font=font)
            label = f'{int(row["index"]):02d}  {row["name"]}'
            draw.rectangle((x, y + frame_h, x + tile_w, y + frame_h + label_h), fill="#111315")
            draw.text((x + 12, y + frame_h + 5), label, fill="white", font=font)
            status_color = "#81c995" if row["status"] == "frame_ok" else "#ff8a80"
            draw.text((x + tile_w - 115, y + frame_h + 14), str(row["status"]), fill=status_color, font=small_font)
        page_number = page_index // page_size + 1
        sheet.save(output_dir / f"contact-sheet-{page_number}.jpg", quality=90)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", default="origin/master:tv-easy.m3u")
    parser.add_argument("--group", default="卫视台")
    parser.add_argument("--output", type=Path, default=Path("/tmp/tv-frame-audit"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=28)
    args = parser.parse_args()

    playlist_path = Path(args.playlist)
    if playlist_path.exists():
        playlist = playlist_path.read_text(encoding="utf-8")
    else:
        playlist = subprocess.run(
            ["git", "show", args.playlist], check=True, capture_output=True, text=True
        ).stdout
    entries = parse_group(playlist, args.group)
    args.output.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str | int]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_capture, entry, args.output, args.timeout): entry for entry in entries}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(f'{int(row["index"]):02d} {row["status"]}: {row["name"]}', flush=True)
    rows.sort(key=lambda row: int(row["index"]))

    with (args.output / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index", "name", "url", "status", "frame", "error"])
        writer.writeheader()
        writer.writerows(rows)
    make_sheets(rows, args.output)
    failures = sum(row["status"] != "frame_ok" for row in rows)
    print(f"captured={len(rows) - failures} failed={failures} total={len(rows)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
