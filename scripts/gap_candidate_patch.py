#!/usr/bin/env python3
"""Candidate-only gap routes and expiring-token safety checks."""
from __future__ import annotations

import re
import time
from types import ModuleType

# Small, targeted operator set for the core gaps reported by candidate run #13.
# These are probe inputs only. Labels never bypass decoded-resolution, bitrate,
# repeated-check, duplicate-content, or frame-review gates.
TARGETED_GAP_EXTRAS = (
    ("CCTV-4", "http://39.134.33.101:6610/270000001128/9900000503/index.m3u8?channel-id=newtv&Contentid=9900000503&livemode=1"),
    ("CCTV-7", "http://39.134.33.101:6610/270000001128/9900000504/index.m3u8?channel-id=newtv&Contentid=9900000504&livemode=1"),
    ("CCTV-11", "http://39.134.33.101:6610/270000001128/9900000508/index.m3u8?channel-id=newtv&Contentid=9900000508&livemode=1"),
    ("CCTV-14", "http://39.134.33.101:6610/270000001128/9900000511/index.m3u8?channel-id=newtv&Contentid=9900000511&livemode=1"),
    ("CCTV-4", "http://huaweicdn.hb.chinamobile.com/PLTV/88888888/224/3221226683/1.m3u8"),
    ("CCTV-7", "http://huaweicdn.hb.chinamobile.com/PLTV/88888888/224/3221226687/1.m3u8"),
    ("CCTV-11", "http://huaweicdn.hb.chinamobile.com/PLTV/88888888/224/3221226684/1.m3u8"),
    ("CCTV-14", "http://huaweicdn.hb.chinamobile.com/PLTV/88888888/224/3221226685/1.m3u8"),
    ("CCTV-4K", "http://222.85.69.6/PLTV/88888888/224/3221227244/index.m3u8"),
    ("湖北卫视", "http://39.134.33.101:6610/270000001128/9900000522/index.m3u8?channel-id=newtv&Contentid=9900000522&livemode=1"),
    ("甘肃卫视", "http://39.134.33.101:6610/270000001128/9900000023/index.m3u8?channel-id=newtv&Contentid=9900000023&livemode=1"),
    ("海南卫视", "http://39.134.33.101:6610/270000001128/9900000037/index.m3u8?channel-id=newtv&Contentid=9900000037&livemode=1"),
    ("东南卫视", "http://39.134.33.101:6610/270000001128/9900000519/index.m3u8?channel-id=newtv&Contentid=9900000519&livemode=1"),
    ("延边卫视", "http://39.134.33.101:6610/270000001128/9900000073/index.m3u8?channel-id=newtv&Contentid=9900000073&livemode=1"),
    ("农林卫视", "http://39.134.33.101:6610/270000001128/9900000060/index.m3u8?channel-id=newtv&Contentid=9900000060&livemode=1"),
    ("三沙卫视", "http://www.hiiptv.cn:6060/000000001000/4600001000000000117/1.m3u8?Contentid=4600001000000000117&stbId=005103FF00010060000100E400F75DA4"),
)

# CCTV and several Chinese CDNs commonly use auth_key=<epoch>-... rather than
# exp=/expires=. Treat an epoch less than 24 hours away as non-promotable.
AUTH_KEY_EXPIRY_RE = re.compile(
    r"(?:^|[?&])(?:auth_key|authkey)=([0-9]{9,13})(?=[-_&]|$)", re.I
)


def auth_key_expires_soon(url: str, *, now: int | None = None) -> bool:
    match = AUTH_KEY_EXPIRY_RE.search(url)
    if not match:
        return False
    raw = int(match.group(1))
    expiry = raw // 1000 if raw > 10_000_000_000 else raw
    current = int(time.time()) if now is None else int(now)
    return expiry - current < 24 * 3600


def apply(bp: ModuleType) -> None:
    """Add only gap candidates and harden short-token detection."""
    for name, url in TARGETED_GAP_EXTRAS:
        extra = (name, "大陆", url)
        if extra not in bp.EXTRAS:
            bp.EXTRAS.append(extra)

    if getattr(bp.has_short_token, "_candidate_auth_key_patch", False):
        return
    original_has_short_token = bp.has_short_token

    def has_short_token(url: str) -> bool:
        return auth_key_expires_soon(url) or original_has_short_token(url)

    has_short_token._candidate_auth_key_patch = True  # type: ignore[attr-defined]
    bp.has_short_token = has_short_token
