#!/usr/bin/env python3
"""One-shot, idempotent patcher for decoded-quality candidate selection.

This file exists so the hardening can be applied by a narrowly scoped Actions
workflow without ever checking out or publishing the four production playlists.
"""
from __future__ import annotations

import re
from pathlib import Path

TARGET = Path("scripts/build_playlist.py")
MARKER = "DECODED_QUALITY_POLICY_V4"


def replace_func(source: str, name: str, body: str) -> str:
    pattern = re.compile(rf"^def {re.escape(name)}\b.*?(?=^def |\Z)", re.M | re.S)
    match = pattern.search(source)
    if not match:
        raise SystemExit(f"cannot find function: {name}")
    return source[: match.start()] + body.rstrip() + "\n\n\n" + source[match.end() :]


HELPERS = r'''
# DECODED_QUALITY_POLICY_V4
# GitHub-hosted runners are outside mainland China. For mainland routes,
# throughput/startup values are diagnostics only. Picture identity and quality
# still have to be proven from the actual media stream.
MAINLAND_ROUTE_GROUPS = {"大陆", "中文付费", "其他地方", *MAINLAND_REGION_GROUPS}
TOKEN_EXPIRY_RE = re.compile(r"(?:^|[?&])(?:exp|expires|expire|expiry)=([0-9]{9,13})(?:&|$)", re.I)


def is_mainland_route(channel: Channel) -> bool:
    return is_core_channel(channel) or channel.group in MAINLAND_ROUTE_GROUPS


def required_actual_height(channel: Channel) -> int:
    if channel_key(channel) == "cctv4k":
        return 2160
    if is_core_channel(channel):
        return 1080
    return 720


def actual_height(channel: Channel) -> int:
    return int(channel.probe.get("decoded_height") or channel.probe.get("height") or 0)


def parse_rate(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        if "/" in value:
            left, right = value.split("/", 1)
            return float(left) / float(right) if float(right) else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def ffprobe_video(url: str, headers: dict[str, str]) -> dict:
    command = [
        "ffprobe", "-v", "error",
        "-rw_timeout", str(int(PROBE_TIMEOUT * 1_000_000)),
        "-user_agent", headers.get("User-Agent", DEFAULT_UA),
        "-analyzeduration", "7000000", "-probesize", "7000000",
    ]
    extra_headers = [
        f"{key}: {value}"
        for key, value in headers.items()
        if key.lower() != "user-agent"
    ]
    if extra_headers:
        command.extend(["-headers", "\r\n".join(extra_headers) + "\r\n"])
    command.extend([
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,field_order,bit_rate:format=bit_rate",
        "-of", "json", url,
    ])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT + 5,
        check=False,
    )
    if completed.returncode:
        raise ValueError("ffprobe:" + (completed.stderr or "failed").strip()[-160:])
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError("ffprobe_no_video")
    stream = streams[0]
    fmt = payload.get("format") or {}
    field_order = str(stream.get("field_order") or "unknown").lower()
    scan = (
        "interlaced" if field_order in {"tt", "bb", "tb", "bt"}
        else "progressive" if field_order == "progressive"
        else "unknown"
    )
    bit_rate = int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": str(stream.get("codec_name") or "unknown").lower(),
        "fps": round(parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate")), 3),
        "field_order": field_order,
        "scan": scan,
        "stream_mbps": round(bit_rate / 1_000_000, 3) if bit_rate else 0.0,
    }


def has_short_token(url: str) -> bool:
    match = TOKEN_EXPIRY_RE.search(url)
    if not match:
        return False
    raw = int(match.group(1))
    expiry = raw // 1000 if raw > 10_000_000_000 else raw
    return expiry - int(time.time()) < 24 * 3600


def publication_url(channel: Channel) -> str:
    return str(channel.probe.get("publish_url") or channel.url)


def clone_with_resolution_label(channel: Channel) -> Channel:
    height = actual_height(channel)
    suffix = f"[{height}p]" if height else "[未知分辨率]"
    name = canonical_display_name(channel)
    if suffix in name:
        return channel
    display = f"{name} {suffix}"
    extinf = channel.extinf
    if "," in extinf:
        extinf = extinf.rsplit(",", 1)[0] + "," + display
    return Channel(
        name=display,
        extinf=extinf,
        url=channel.url,
        group=channel.group,
        allow_geo=channel.allow_geo,
        curated=channel.curated,
        headers=dict(channel.headers),
        static_score=channel.static_score,
        probe=dict(channel.probe),
        display_override=display,
        source=channel.source,
        history=dict(channel.history),
    )
'''


CHOOSE_VARIANT = r'''
def choose_variant(rows: list[tuple[int, int, int, str]]) -> tuple[int, int, int, str] | None:
    """Return only a probe-order hint; manifest labels are never quality proof."""
    if not rows:
        return None
    normal = [row for row in rows if 720 <= row[1] <= 1080 and (not row[2] or row[2] <= 14_000_000)]
    if normal:
        return max(normal, key=lambda row: (row[1], row[0], row[2]))
    with_size = [row for row in rows if row[1] > 0]
    if with_size:
        return max(with_size, key=lambda row: (row[1], row[0], row[2]))
    return rows[0]
'''


PROBE_ONCE = r'''
def probe_once(channel: Channel, use_declared_headers: bool) -> dict:
    headers = channel.headers if use_declared_headers else {}
    manifest_bytes, manifest_seconds, final_url = timed_read(channel.url, headers, 512 * 1024, PROBE_TIMEOUT)
    text = manifest_bytes.decode("utf-8", "ignore")

    # Direct transport streams and redirects without an HLS manifest are valid
    # inputs. Their dimensions come only from ffprobe on the actual video.
    if "#EXTM3U" not in text:
        decoded = ffprobe_video(final_url, headers)
        payload, elapsed, direct_final, resource_size = timed_read_with_size(
            final_url, headers, MAX_PROBE_BYTES, PROBE_TIMEOUT
        )
        if len(payload) < 16 * 1024:
            raise ValueError("short_direct_stream")
        speed_mbps = len(payload) * 8 / elapsed / 1_000_000
        return {
            "ok": True,
            "manifest_s": round(manifest_seconds, 3),
            "segment_mbps": round(speed_mbps, 2),
            "segment_bytes": len(payload),
            "segment_sha256": hashlib.sha256(payload).hexdigest(),
            "stream_mbps": decoded["stream_mbps"],
            "width": decoded["width"],
            "height": decoded["height"],
            "decoded_width": decoded["width"],
            "decoded_height": decoded["height"],
            "codec": decoded["codec"],
            "fps": decoded["fps"],
            "scan": decoded["scan"],
            "field_order": decoded["field_order"],
            "bandwidth": 0,
            "declared_width": 0,
            "declared_height": 0,
            "is_master_playlist": False,
            "tested_variant_url": direct_final,
            "publish_url": channel.url,
            "variant_actual_heights": [decoded["height"]] if decoded["height"] else [],
            "has_low_variant": 0 < decoded["height"] < 1080,
            "short_lived_token": has_short_token(channel.url) or has_short_token(direct_final),
            "header_required": use_declared_headers,
            "ipv4_dns": url_ip_families(final_url)[0],
            "ipv6_dns": url_ip_families(final_url)[1],
            "dual_stack_dns": all(url_ip_families(final_url)),
        }

    rows = variant_rows(text, final_url)
    upper = text.upper()
    if not rows and ("#EXT-X-ENDLIST" in upper or re.search(r"#EXT-X-PLAYLIST-TYPE\s*:\s*VOD", upper)):
        raise ValueError("vod_playlist")

    variants: list[dict] = []
    if rows:
        hint = choose_variant(rows)
        ordered = ([hint] if hint else []) + [row for row in rows if row != hint]
        for declared_width, declared_height, bandwidth, variant_url in ordered:
            try:
                media_bytes, media_seconds, media_final = timed_read(
                    variant_url, headers, 512 * 1024, PROBE_TIMEOUT
                )
                media_text = media_bytes.decode("utf-8", "ignore")
                if "#EXTM3U" not in media_text:
                    continue
                decoded = ffprobe_video(variant_url, headers)
                variants.append({
                    "declared_width": declared_width,
                    "declared_height": declared_height,
                    "bandwidth": bandwidth,
                    "variant_url": variant_url,
                    "media_final": media_final,
                    "media_text": media_text,
                    "media_seconds": media_seconds,
                    "decoded": decoded,
                })
            except Exception:
                continue
        if not variants:
            raise ValueError("master_no_decodable_variant")
    else:
        decoded = ffprobe_video(final_url, headers)
        variants.append({
            "declared_width": 0,
            "declared_height": 0,
            "bandwidth": 0,
            "variant_url": channel.url,
            "media_final": final_url,
            "media_text": text,
            "media_seconds": 0.0,
            "decoded": decoded,
        })

    required = required_actual_height(channel)
    if is_core_channel(channel):
        # For normal CCTV/satellites prefer the closest representation at or
        # above 1080 rather than accidentally selecting a huge adaptive tier.
        def rank(item: dict) -> tuple:
            height = int(item["decoded"]["height"] or 0)
            if channel_key(channel) == "cctv4k":
                return (height >= 2160, height, item["decoded"]["stream_mbps"])
            return (height >= required, -abs(height - required) if height >= required else height, item["decoded"]["stream_mbps"])
    else:
        def rank(item: dict) -> tuple:
            height = int(item["decoded"]["height"] or 0)
            return (height >= 720, min(height, 1080), item["decoded"]["stream_mbps"])

    selected = max(variants, key=rank)
    decoded = selected["decoded"]
    media_text = selected["media_text"]
    if "#EXT-X-ENDLIST" in media_text.upper() or re.search(
        r"#EXT-X-PLAYLIST-TYPE\s*:\s*VOD", media_text.upper()
    ):
        raise ValueError("vod_playlist")
    segment_url, segment_duration = first_media_segment(media_text, selected["media_final"])
    if not segment_url:
        raise ValueError("no_live_segment")
    segment, segment_seconds, _, segment_resource_bytes = timed_read_with_size(
        segment_url, headers, MAX_PROBE_BYTES, PROBE_TIMEOUT
    )
    if len(segment) < 16 * 1024:
        raise ValueError("short_segment")
    speed_mbps = len(segment) * 8 / segment_seconds / 1_000_000
    estimated_stream_mbps = (
        segment_resource_bytes * 8 / segment_duration / 1_000_000
        if segment_resource_bytes and segment_duration > 0 else 0.0
    )
    stream_mbps = decoded["stream_mbps"] or estimated_stream_mbps
    publish_url = selected["variant_url"] if rows else channel.url
    if channel_key(channel) == "cctv8" and re.search(
        r"(?:cctv[-_]?8k|cctv8k|/8k(?:[/?.]|$))", urllib.parse.unquote(publish_url).lower()
    ):
        raise ValueError("cctv8_identity_conflict")

    manifest_ipv4, manifest_ipv6 = url_ip_families(final_url)
    segment_ipv4, segment_ipv6 = url_ip_families(segment_url)
    actual_heights = sorted({int(item["decoded"]["height"] or 0) for item in variants if int(item["decoded"]["height"] or 0) > 0})
    return {
        "ok": True,
        "manifest_s": round(manifest_seconds + float(selected["media_seconds"] or 0), 3),
        "segment_mbps": round(speed_mbps, 2),
        "segment_bytes": len(segment),
        "segment_sha256": hashlib.sha256(segment).hexdigest(),
        "stream_mbps": round(float(stream_mbps or 0), 3),
        "width": decoded["width"],
        "height": decoded["height"],
        "decoded_width": decoded["width"],
        "decoded_height": decoded["height"],
        "codec": decoded["codec"],
        "fps": decoded["fps"],
        "scan": decoded["scan"],
        "field_order": decoded["field_order"],
        "bandwidth": int(selected["bandwidth"] or 0),
        "declared_width": int(selected["declared_width"] or 0),
        "declared_height": int(selected["declared_height"] or 0),
        "is_master_playlist": bool(rows),
        "tested_variant_url": selected["variant_url"],
        "publish_url": publish_url,
        "variant_actual_heights": actual_heights,
        "has_low_variant": any(0 < height < 1080 for height in actual_heights),
        "short_lived_token": has_short_token(publish_url) or has_short_token(selected["media_final"]),
        "header_required": use_declared_headers,
        "ipv4_dns": manifest_ipv4 or segment_ipv4,
        "ipv6_dns": manifest_ipv6 or segment_ipv6,
        "dual_stack_dns": (manifest_ipv4 or segment_ipv4) and (manifest_ipv6 or segment_ipv6),
    }
'''


IS_STABLE = r'''
def is_stable(channel: Channel) -> bool:
    if is_placeholder_relay(channel):
        return False
    probe = channel.probe
    if (
        not probe.get("ok")
        or probe.get("header_required")
        or probe.get("duplicate_core_content")
        or probe.get("short_lived_token")
    ):
        return False
    height = actual_height(channel)
    if not height or height < required_actual_height(channel):
        return False
    if probe.get("is_master_playlist") and publication_url(channel) == channel.url:
        return False
    stream_mbps = float(probe.get("stream_mbps") or 0)
    if is_core_channel(channel) and str(probe.get("codec") or "").lower() in {"h264", "avc", "avc1"} and stream_mbps and stream_mbps < 2.0:
        return False
    if is_mainland_route(channel):
        return True
    speed = float(probe.get("segment_mbps") or 0)
    latency = float(probe.get("manifest_s") or 99)
    bandwidth_mbps = float(probe.get("bandwidth") or 0) / 1_000_000
    required_speed = max(2.2, bandwidth_mbps * 1.25)
    if stream_mbps:
        required_speed = max(required_speed, stream_mbps * (1.35 if is_core_channel(channel) else 1.20))
    return latency <= 5.0 and speed >= required_speed
'''


CORE_ACCEPTABLE = r'''
def is_core_acceptable(channel: Channel) -> bool:
    """Core promotion gate: decoded 1080 minimum, 2160 for CCTV-4K."""
    if not is_core_channel(channel) or is_placeholder_relay(channel):
        return False
    probe = channel.probe
    if (
        not probe.get("ok")
        or probe.get("header_required")
        or probe.get("duplicate_core_content")
        or probe.get("short_lived_token")
    ):
        return False
    height = actual_height(channel)
    if not height or height < required_actual_height(channel):
        return False
    if probe.get("is_master_playlist") and publication_url(channel) == channel.url:
        return False
    stream_mbps = float(probe.get("stream_mbps") or 0)
    codec = str(probe.get("codec") or "").lower()
    if codec in {"h264", "avc", "avc1"} and stream_mbps and stream_mbps < 2.0:
        return False
    return True
'''


EASY_READY = r'''
def is_easy_ready(channel: Channel) -> bool:
    """Family list requires three fresh checks and decoded picture quality."""
    probe = channel.probe
    if (
        not is_stable(channel)
        or int(probe.get("checks_ok") or 0) < 3
        or probe.get("recheck_failed")
        or probe.get("easy_check_failed")
        or probe.get("geo_restricted")
    ):
        return False
    if is_mainland_route(channel):
        return True
    recent = [1 if value else 0 for value in channel.history.get("recent", [])][-HISTORY_WINDOW:]
    return not (len(recent) >= 4 and sum(recent) / len(recent) < 0.75)
'''


FAMILY_CORE = r'''
def is_family_core_usable(channel: Channel) -> bool:
    """Two-check core fallback, but never a 720/unknown core primary."""
    if not is_core_acceptable(channel):
        return False
    probe = channel.probe
    return (
        int(probe.get("checks_ok") or 0) >= 2
        and not probe.get("recheck_failed")
        and not probe.get("geo_restricted")
    )
'''


CORE_SCORE = r'''
def core_route_score(channel: Channel) -> float:
    """Rank core routes by decoded picture/bitrate; mainland speed is advisory."""
    probe = channel.probe
    if not is_core_acceptable(channel):
        return -9999.0
    height = actual_height(channel)
    stream_mbps = float(probe.get("stream_mbps") or 0)
    if height >= 2160:
        score = 220.0
    elif height >= 1080:
        score = 160.0
    else:
        return -9999.0
    if stream_mbps:
        score += min(stream_mbps, 12.0) * 32.0
    score += min(int(probe.get("checks_ok") or 1), 3) * 20.0
    score += max(-60.0, min(historical_score(channel), 90.0)) * (0.08 if is_mainland_route(channel) else 0.30)
    score += max(-80.0, min(channel.static_score, 80.0)) * 0.10
    if not is_mainland_route(channel):
        speed = float(probe.get("segment_mbps") or 0)
        latency = float(probe.get("manifest_s") or 10)
        if stream_mbps:
            headroom = speed / stream_mbps if stream_mbps else 0
            if headroom >= 1.5:
                score += 35
            elif headroom < 1.35:
                score -= 160
        score -= latency * 6.0
    return score
'''


SELECT_STABLE = r'''
def select_stable(channels: list[Channel]) -> list[Channel]:
    """Select only decoded-quality routes; never pad core with 720/unknown."""
    best: dict[str, Channel] = {}
    for channel in channels:
        if not (is_core_acceptable(channel) if is_core_channel(channel) else is_stable(channel)):
            continue
        key = channel_key(channel)
        existing = best.get(key)
        if existing is None or route_selection_score(channel) > route_selection_score(existing):
            best[key] = channel

    eligible = list(best.values())
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in eligible:
        grouped[display_group(channel)].append(channel)
    for items in grouped.values():
        items.sort(key=route_selection_score, reverse=True)

    selected: list[Channel] = []
    selected_keys: set[str] = set()
    for channel in sorted((item for item in eligible if is_core_channel(item)), key=core_route_score, reverse=True):
        key = channel_key(channel)
        if key not in selected_keys:
            selected.append(channel)
            selected_keys.add(key)

    for group, quota in GROUP_TARGETS.items():
        already = sum(display_group(channel) == group for channel in selected)
        for channel in grouped.get(group, []):
            if already >= quota or len(selected) >= TARGET_STABLE:
                break
            key = channel_key(channel)
            if key in selected_keys:
                continue
            selected.append(channel)
            selected_keys.add(key)
            already += 1

    if len(selected) < TARGET_STABLE:
        overflow = sorted(
            (channel for channel in eligible if channel_key(channel) not in selected_keys),
            key=route_selection_score,
            reverse=True,
        )
        selected.extend(overflow[: TARGET_STABLE - len(selected)])
    return selected[:TARGET_STABLE]
'''


SELECT_EASY = r'''
def select_easy(channels: list[Channel], target: int = TARGET_EASY) -> list[Channel]:
    """Family list: actual >=720, core actual >=1080, no quantity padding."""
    best: dict[str, Channel] = {}
    for channel in channels:
        if channel.display_override:
            continue
        ready = is_easy_ready(channel)
        if not ready and is_core_channel(channel):
            ready = is_family_core_usable(channel)
        if not ready:
            continue
        key = channel_key(channel)
        existing = best.get(key)
        if existing is None or route_selection_score(channel) > route_selection_score(existing):
            best[key] = channel

    eligible = list(best.values())
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in eligible:
        grouped[display_group(channel)].append(channel)
    for items in grouped.values():
        items.sort(key=route_selection_score, reverse=True)

    selected: list[Channel] = []
    selected_keys: set[str] = set()
    for channel in sorted((item for item in eligible if is_core_channel(item)), key=core_route_score, reverse=True):
        key = channel_key(channel)
        if key not in selected_keys:
            selected.append(channel)
            selected_keys.add(key)
    for group, quota in EASY_GROUP_TARGETS.items():
        already = sum(display_group(channel) == group for channel in selected)
        for channel in grouped.get(group, []):
            if already >= quota or len(selected) >= target:
                break
            key = channel_key(channel)
            if key in selected_keys:
                continue
            selected.append(channel)
            selected_keys.add(key)
            already += 1
    if len(selected) < target:
        overflow = sorted(
            (channel for channel in eligible if channel_key(channel) not in selected_keys),
            key=route_selection_score,
            reverse=True,
        )
        selected.extend(overflow[: target - len(selected)])
    return selected[:target]
'''


RESTORE_CORE = r'''
def restore_existing_family_core(
    selected: list[Channel], existing_easy: list[Channel], target: int = TARGET_EASY
) -> list[Channel]:
    """Never carry an old core blindly; only explicit visual-confirmed URLs."""
    output = list(selected)
    selected_keys = {channel_key(channel) for channel in output}
    for channel in existing_easy:
        key = channel_key(channel)
        if key in selected_keys or not is_core_channel(channel):
            continue
        if channel.url not in VISUALLY_CONFIRMED_CORE_URLS:
            continue
        if labelled_height(channel) < 1080 and channel_key(channel) != "cctv4k":
            continue
        channel.probe["manual_1080_confirmed"] = True
        channel.probe["carried_family_fallback"] = True
        output.append(channel)
        selected_keys.add(key)
    if len(output) <= target:
        return output
    core = [channel for channel in output if is_core_channel(channel)]
    ordinary = sorted((channel for channel in output if not is_core_channel(channel)), key=measured_score, reverse=True)
    return core + ordinary[: max(0, target - len(core))]
'''


SELECT_ALL = r'''
def select_all(channels: list[Channel], stable: list[Channel]) -> list[Channel]:
    """Broader fallback may keep low/unknown routes, always visibly labelled."""
    selected = list(stable)
    keys = {channel_key(channel) for channel in selected}
    best_remainder: dict[str, Channel] = {}
    for channel in channels:
        key = channel_key(channel)
        if key in keys or is_placeholder_relay(channel):
            continue
        existing = best_remainder.get(key)
        if existing is None or publication_fallback_rank(channel) > publication_fallback_rank(existing):
            best_remainder[key] = channel
    remainder = sorted(best_remainder.values(), key=publication_fallback_rank, reverse=True)
    for channel in remainder[: max(0, TARGET_ALL - len(selected))]:
        if actual_height(channel) and actual_height(channel) >= 720:
            selected.append(channel)
        else:
            selected.append(clone_with_resolution_label(channel))
    return selected[:TARGET_ALL]
'''


SELECT_MAIN = r'''
def select_main(stable: list[Channel], full: list[Channel], target: int = TARGET_MAIN) -> list[Channel]:
    """Main list may broaden ordinary stations but never downgrade a core tile."""
    selected: list[Channel] = []
    selected_keys: set[str] = set()
    for channel in stable:
        key = channel_key(channel)
        if channel.display_override or key in selected_keys:
            continue
        selected.append(channel)
        selected_keys.add(key)
        if len(selected) >= target:
            return selected
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in full:
        key = channel_key(channel)
        if channel.display_override or key in selected_keys:
            continue
        if is_core_channel(channel) and not is_core_acceptable(channel):
            continue
        grouped[display_group(channel)].append(channel)
    for items in grouped.values():
        items.sort(key=publication_fallback_rank, reverse=True)
    for group, quota in GROUP_TARGETS.items():
        already = sum(display_group(channel) == group for channel in selected)
        for channel in grouped.get(group, []):
            if already >= quota or len(selected) >= target:
                break
            key = channel_key(channel)
            if key in selected_keys:
                continue
            selected.append(channel)
            selected_keys.add(key)
            already += 1
        if len(selected) >= target:
            return selected
    overflow = sorted(
        (
            channel for channel in full
            if not channel.display_override
            and channel_key(channel) not in selected_keys
            and (not is_core_channel(channel) or is_core_acceptable(channel))
        ),
        key=publication_fallback_rank,
        reverse=True,
    )
    for channel in overflow:
        key = channel_key(channel)
        if key in selected_keys:
            continue
        selected.append(channel)
        selected_keys.add(key)
        if len(selected) >= target:
            break
    return selected
'''


WRITE_PLAYLIST = r'''
def write_playlist(path: Path, channels: list[Channel], description: str) -> None:
    lines = [
        f'#EXTM3U x-tvg-url="{EPG_URL}"',
        f"# {description}",
        f"# generated_utc={GENERATED_AT}",
        f"# channels={len(channels)}",
    ]
    for channel in sort_channels(channels):
        lines.extend((cleaned_extinf(channel), publication_url(channel)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
'''


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")
    if MARKER in source:
        print("decoded quality hardening already applied")
        return 0
    if "import subprocess\n" not in source:
        source = source.replace("import socket\n", "import socket\nimport subprocess\n", 1)
    anchor = "def channel_static_score(channel: Channel) -> float:\n"
    if anchor not in source:
        raise SystemExit("helper insertion anchor missing")
    source = source.replace(anchor, HELPERS.rstrip() + "\n\n\n" + anchor, 1)
    replacements = {
        "choose_variant": CHOOSE_VARIANT,
        "probe_once": PROBE_ONCE,
        "is_stable": IS_STABLE,
        "is_core_acceptable": CORE_ACCEPTABLE,
        "is_easy_ready": EASY_READY,
        "is_family_core_usable": FAMILY_CORE,
        "core_route_score": CORE_SCORE,
        "select_stable": SELECT_STABLE,
        "select_easy": SELECT_EASY,
        "restore_existing_family_core": RESTORE_CORE,
        "select_all": SELECT_ALL,
        "select_main": SELECT_MAIN,
        "write_playlist": WRITE_PLAYLIST,
    }
    for name, body in replacements.items():
        source = replace_func(source, name, body)
    compile(source, str(TARGET), "exec")
    TARGET.write_text(source, encoding="utf-8", newline="\n")
    print("applied decoded quality hardening")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
