#!/usr/bin/env python3
"""Candidate-only ranking corrections for mainland direct-play routes.

GitHub-hosted runners are outside mainland China. For mainland routes, decoded
quality, intrinsic programme bitrate and repeat probe success decide ranking;
runner throughput/startup remain diagnostics only. This module patches ranking
only inside the isolated candidate builder and never publishes production files.
"""
from __future__ import annotations

from types import ModuleType

HOTEL_SOURCE = (
    "大陆",
    "https://raw.githubusercontent.com/cymz6/AutoIPTV-Hotel/main/lives.m3u",
    False,
)

# Manually retained operator routes from the user's previously supplied Tianjin
# playlist. Only the primary CCTV/satellite block is included; the explicitly
# labelled low-bitrate block is intentionally excluded. These are candidates,
# never trusted metadata: ffprobe still decides decoded size/codec/bitrate.
TIANJIN_OPERATOR_EXTRAS = (
    ("CCTV-1", "http://111.32.21.78/PLTV/88888888/224/3221226366/1.m3u8"),
    ("CCTV-2", "http://111.32.21.78/PLTV/88888888/224/3221226385/1.m3u8"),
    ("CCTV-3", "http://111.32.21.78/PLTV/88888888/224/3221226518/1.m3u8"),
    ("CCTV-4", "http://111.32.21.78/PLTV/88888888/224/3221226388/1.m3u8"),
    ("CCTV-5", "http://111.32.21.78/PLTV/88888888/224/3221226537/1.m3u8"),
    ("CCTV-5+", "http://111.32.21.78/PLTV/88888888/224/3221226399/1.m3u8"),
    ("CCTV-6", "http://111.32.21.78/PLTV/88888888/224/3221226540/1.m3u8"),
    ("CCTV-7", "http://111.32.21.78/PLTV/88888888/224/3221226391/1.m3u8"),
    ("CCTV-8", "http://111.32.21.78/PLTV/88888888/224/3221226543/1.m3u8"),
    ("CCTV-9", "http://111.32.21.78/PLTV/88888888/224/3221226404/1.m3u8"),
    ("CCTV-10", "http://111.32.21.78/PLTV/88888888/224/3221226387/1.m3u8"),
    ("CCTV-11", "http://111.32.21.78/PLTV/88888888/224/3221226390/1.m3u8"),
    ("CCTV-12", "http://111.32.21.78/PLTV/88888888/224/3221226395/1.m3u8"),
    ("CCTV-13", "http://111.32.21.78/PLTV/88888888/224/3221226367/1.m3u8"),
    ("CCTV-14", "http://111.32.21.78/PLTV/88888888/224/3221226398/1.m3u8"),
    ("CCTV-15", "http://111.32.21.78/PLTV/88888888/224/3221226401/1.m3u8"),
    ("CCTV-16", "http://111.32.21.78/PLTV/88888888/224/3221226393/1.m3u8"),
    ("CCTV-17", "http://111.32.21.78/PLTV/88888888/224/3221226396/1.m3u8"),
    ("北京卫视", "http://111.32.21.78/PLTV/88888888/224/3221226403/1.m3u8"),
    ("东方卫视", "http://111.32.21.78/PLTV/88888888/224/3221226372/1.m3u8"),
    ("重庆卫视", "http://111.32.21.78/PLTV/88888888/224/3221226369/1.m3u8"),
    ("河北卫视", "http://111.32.21.78/PLTV/88888888/224/3221226430/1.m3u8"),
    ("山西卫视", "http://111.32.21.78/PLTV/88888888/224/3221226465/1.m3u8"),
    ("内蒙古卫视", "http://111.32.21.78/PLTV/88888888/224/3221226458/1.m3u8"),
    ("辽宁卫视", "http://111.32.21.78/PLTV/88888888/224/3221226450/1.m3u8"),
    ("吉林卫视", "http://111.32.21.78/PLTV/88888888/224/3221226429/1.m3u8"),
    ("黑龙江卫视", "http://111.32.21.78/PLTV/88888888/224/3221226423/1.m3u8"),
    ("江苏卫视", "http://111.32.21.78/PLTV/88888888/224/3221226442/1.m3u8"),
    ("浙江卫视", "http://111.32.21.78/PLTV/88888888/224/3221226377/1.m3u8"),
    ("安徽卫视", "http://111.32.21.78/PLTV/88888888/224/3221226400/1.m3u8"),
    ("东南卫视", "http://111.32.21.78/PLTV/88888888/224/3221226415/1.m3u8"),
    ("江西卫视", "http://111.32.21.78/PLTV/88888888/224/3221226437/1.m3u8"),
    ("山东卫视", "http://111.32.21.78/PLTV/88888888/224/3221226374/1.m3u8"),
    ("河南卫视", "http://111.32.21.78/PLTV/88888888/224/3221226434/1.m3u8"),
    ("湖北卫视", "http://111.32.21.78/PLTV/88888888/224/3221226426/1.m3u8"),
    ("湖南卫视", "http://111.32.21.78/PLTV/88888888/224/3221226439/1.m3u8"),
    ("广东卫视", "http://111.32.21.78/PLTV/88888888/224/3221226421/1.m3u8"),
    ("广西卫视", "http://111.32.21.78/PLTV/88888888/224/3221226424/1.m3u8"),
    ("海南卫视", "http://111.32.21.78/PLTV/88888888/224/3221226428/1.m3u8"),
    ("四川卫视", "http://111.32.21.78/PLTV/88888888/224/3221226467/1.m3u8"),
    ("贵州卫视", "http://111.32.21.78/PLTV/88888888/224/3221226427/1.m3u8"),
    ("云南卫视", "http://111.32.21.78/PLTV/88888888/224/3221226478/1.m3u8"),
    ("西藏卫视", "http://111.32.21.78/PLTV/88888888/224/3221226460/1.m3u8"),
    ("陕西卫视", "http://111.32.21.78/PLTV/88888888/224/3221226468/1.m3u8"),
    ("甘肃卫视", "http://111.32.21.78/PLTV/88888888/224/3221226417/1.m3u8"),
    ("青海卫视", "http://111.32.21.78/PLTV/88888888/224/3221226456/1.m3u8"),
    ("宁夏卫视", "http://111.32.21.78/PLTV/88888888/224/3221226453/1.m3u8"),
    ("新疆卫视", "http://111.32.21.78/PLTV/88888888/224/3221226463/1.m3u8"),
    ("深圳卫视", "http://111.32.21.78/PLTV/88888888/224/3221226464/1.m3u8"),
    ("厦门卫视", "http://111.32.21.78/PLTV/88888888/224/3221226457/1.m3u8"),
    ("兵团卫视", "http://111.32.21.78/PLTV/88888888/224/3221226406/1.m3u8"),
    ("延边卫视", "http://111.32.21.78/PLTV/88888888/224/3221226472/1.m3u8"),
    ("大湾区卫视", "http://111.32.21.78/PLTV/88888888/224/3221226425/1.m3u8"),
    ("安多卫视", "http://111.32.21.78/PLTV/88888888/224/3221226410/1.m3u8"),
    ("农林卫视", "http://111.32.21.78/PLTV/88888888/224/3221226486/1.m3u8"),
)


def apply(bp: ModuleType) -> None:
    """Patch candidate ranking without changing overseas network policy."""
    if HOTEL_SOURCE not in bp.SOURCES:
        bp.SOURCES.append(HOTEL_SOURCE)
    for name, url in TIANJIN_OPERATOR_EXTRAS:
        extra = (name, "大陆", url)
        if extra not in bp.EXTRAS:
            bp.EXTRAS.append(extra)

    original_measured_score = bp.measured_score
    original_fallback_rank = bp.publication_fallback_rank
    original_add_cctv5_backups = bp.add_cctv5_backups
    original_merge_probe_results = bp.merge_probe_results

    def mainland_quality_score(channel) -> float:
        probe = channel.probe
        if probe.get("geo_restricted"):
            return -9000.0
        if (
            not probe.get("ok")
            or probe.get("header_required")
            or probe.get("duplicate_core_content")
            or probe.get("short_lived_token")
        ):
            return -9999.0

        height = int(bp.actual_height(channel) or 0)
        stream_mbps = float(probe.get("stream_mbps") or 0.0)
        codec = str(probe.get("codec") or "").lower()
        checks = int(probe.get("checks_ok") or 0)

        score = max(-80.0, min(float(channel.static_score or 0.0), 80.0)) * 0.10
        score += max(-60.0, min(float(bp.historical_score(channel)), 90.0)) * 0.08

        if height >= 2160:
            score += 260.0
        elif height >= 1080:
            score += 190.0
        elif height >= 720:
            score += 80.0
        elif height > 0:
            score -= 120.0
        else:
            score -= 220.0

        if stream_mbps:
            score += min(stream_mbps, 12.0) * 16.0
            if height >= 1080 and codec in {"h264", "avc", "avc1"} and stream_mbps < 2.0:
                score -= 220.0

        if checks >= 3:
            score += 65.0
        elif checks >= 2:
            score += 40.0
        elif probe.get("recheck_failed"):
            score -= 70.0

        if probe.get("ipv6_dns"):
            score += 8.0
        if probe.get("dual_stack_dns"):
            score += 4.0
        return score

    def measured_score(channel) -> float:
        if bp.is_mainland_route(channel):
            return mainland_quality_score(channel)
        return original_measured_score(channel)

    def publication_fallback_rank(channel) -> tuple:
        if not bp.is_mainland_route(channel):
            return original_fallback_rank(channel)
        probe = channel.probe
        ok = bool(probe.get("ok")) and not bool(
            probe.get("header_required") or probe.get("duplicate_core_content")
        )
        height = int(bp.actual_height(channel) or 0)
        return (
            ok and int(probe.get("checks_ok") or 0) >= 2,
            ok,
            str(channel.source).startswith("carried:"),
            channel.curated,
            height >= 720,
            mainland_quality_score(channel) if ok else float(channel.static_score or 0.0),
        )

    def valid_cctv5_backup(channel) -> bool:
        if bp.channel_key(channel) != "cctv5":
            return True
        probe = channel.probe
        if (
            not probe.get("ok")
            or probe.get("header_required")
            or probe.get("duplicate_core_content")
            or probe.get("short_lived_token")
            or bp.cctv_url_conflicts_with_label(channel)
        ):
            return False
        if int(bp.actual_height(channel) or 0) < 1080:
            return False
        if probe.get("is_master_playlist") and bp.publication_url(channel) == channel.url:
            return False
        stream_mbps = float(probe.get("stream_mbps") or 0.0)
        codec = str(probe.get("codec") or "").lower()
        if codec in {"h264", "avc", "avc1"} and stream_mbps and stream_mbps < 2.0:
            return False
        return True

    def add_cctv5_backups(stable, channels, count: int = 2):
        filtered = [channel for channel in channels if valid_cctv5_backup(channel)]
        return original_add_cctv5_backups(stable, filtered, count=count)

    def merge_probe_results(first: dict, later: dict, checks_ok: int) -> dict:
        """Carry the worst decoded size across rechecks, not just legacy height."""
        combined = original_merge_probe_results(first, later, checks_ok)
        heights = [
            int(value)
            for value in (
                first.get("decoded_height") or first.get("height"),
                later.get("decoded_height") or later.get("height"),
            )
            if value and int(value) > 0
        ]
        widths = [
            int(value)
            for value in (
                first.get("decoded_width") or first.get("width"),
                later.get("decoded_width") or later.get("width"),
            )
            if value and int(value) > 0
        ]
        if heights:
            worst_height = min(heights)
            combined["decoded_height"] = worst_height
            combined["height"] = worst_height
        if widths:
            combined["decoded_width"] = min(widths)
        return combined

    bp.measured_score = measured_score
    bp.publication_fallback_rank = publication_fallback_rank
    bp.add_cctv5_backups = add_cctv5_backups
    bp.merge_probe_results = merge_probe_results
