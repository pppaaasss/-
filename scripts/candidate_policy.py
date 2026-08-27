#!/usr/bin/env python3
"""Candidate-only ranking corrections for mainland direct-play routes.

GitHub-hosted runners are outside mainland China. For mainland routes, decoded
quality, intrinsic programme bitrate and repeat probe success decide ranking;
runner throughput/startup remain diagnostics only. This module patches ranking
only inside the isolated candidate builder and never publishes production files.
"""
from __future__ import annotations

from types import ModuleType


def apply(bp: ModuleType) -> None:
    """Patch candidate ranking without changing overseas network policy."""
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

        # Keep static/history influence deliberately small. They may help break
        # ties but cannot beat decoded picture quality.
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

        # Intrinsic bitrate is a picture-quality signal. Do not use runner
        # download speed/headroom here.
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
        # The legacy helper looked at probe.height/labels. Feed it only routes
        # that have passed decoded-quality checks so fake 1080 backups cannot
        # displace ordinary channels in the candidate list.
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
