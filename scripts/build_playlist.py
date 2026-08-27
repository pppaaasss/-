#!/usr/bin/env python3
"""Build APTV playlists with real HLS manifest and media-segment probes.

The main playlist (tv.m3u) favors fast 720p/1080p streams.  A broader
tv-all.m3u is kept as a fallback so that a transient probe failure does not
make a channel disappear completely.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import functools
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from channel_regions import (
        MAINLAND_REGION_GROUPS,
        group_sort_index,
        regionalized_group,
    )
except ModuleNotFoundError:  # Imported by the repository's offline tests.
    from scripts.channel_regions import (
        MAINLAND_REGION_GROUPS,
        group_sort_index,
        regionalized_group,
    )


# Keep tv.m3u as the permanent APTV subscription path; only its contents change.
TARGET_STABLE = int(os.getenv("TARGET_STABLE", "420"))
MIN_STABLE = int(os.getenv("MIN_STABLE", "280"))
TARGET_MAIN = int(os.getenv("TARGET_MAIN", "450"))
MIN_MAIN = int(os.getenv("MIN_MAIN", "430"))
TARGET_ALL = int(os.getenv("TARGET_ALL", "560"))
TARGET_EASY = int(os.getenv("TARGET_EASY", "180"))
# The family list may shrink to the CCTV + satellite essentials when unrelated
# sources are weak. Movie/music/overseas availability must never block it.
MIN_EASY = int(os.getenv("MIN_EASY", "55"))
SOURCE_WORKERS = int(os.getenv("SOURCE_WORKERS", "12"))
PROBE_WORKERS = int(os.getenv("PROBE_WORKERS", "44"))
PROBE_TIMEOUT = float(os.getenv("PROBE_TIMEOUT", "9"))
MAX_VARIANTS_PER_CHANNEL = int(os.getenv("MAX_VARIANTS_PER_CHANNEL", "8"))
CORE_VARIANTS_PER_CHANNEL = int(os.getenv("CORE_VARIANTS_PER_CHANNEL", "16"))
MAX_PROBE_BYTES = 768 * 1024
NOW_UTC = dt.datetime.now(dt.timezone.utc)
GENERATED_AT = NOW_UTC.isoformat(timespec="seconds").replace("+00:00", "Z")
HISTORY_PATH = Path("health-history.json")
HISTORY_WINDOW = int(os.getenv("HISTORY_WINDOW", "12"))

DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV playlist health-check/2.0)"
EPG_URL = "https://live.fanmingming.cn/e.xml"
LOGO_BASE_URL = "https://live.fanmingming.cn/tv"

# Viewer profile: local Chinese, Taiwan and Japan replace the former bulk
# documentary/movie/news catalogue. Missing groups are filled only by another
# wanted language/region; foreign English overflow is never used as padding.
GROUP_TARGETS = {
    # Reserve regional variety before mainland overflow.  The final display
    # order remains CCTV/satellite -> mainland regions -> HK/TW/JP.
    "卫视台": 72,
    "台湾": 60,
    "香港": 25,
    "日本": 20,
    "澳门": 5,
    "新加坡": 2,
    "马来西亚": 3,
    "北京": 10,
    "上海": 16,
    "天津": 8,
    "重庆": 8,
    "河北": 16,
    "山西": 12,
    "内蒙古": 8,
    "辽宁": 10,
    "吉林": 8,
    "黑龙江": 8,
    "江苏": 22,
    "浙江": 22,
    "安徽": 12,
    "福建": 12,
    "江西": 12,
    "山东": 16,
    "河南": 16,
    "湖北": 16,
    "湖南": 16,
    "广东": 24,
    "广西": 10,
    "海南": 8,
    "四川": 16,
    "贵州": 10,
    "云南": 10,
    "西藏": 5,
    "陕西": 10,
    "甘肃": 8,
    "青海": 5,
    "宁夏": 5,
    "新疆": 10,
    "其他地方": 35,
    "中文付费": 30,
    "娱乐": 10,
    "体育": 15,
    "少儿": 10,
    "音乐": 5,
    "教育": 5,
    "财经": 5,
}

# Source buckets are much broader than output regions.  Probe enough unique
# mainland/language entries for a 450-channel publication instead of letting
# the old 80-channel 中文综合 quota starve local stations before classification.
PROBE_GROUP_TARGETS = {
    "大陆": 300,
    "中文综合": 160,
    "中文付费": 50,
    "香港": 35,
    "澳门": 8,
    "台湾": 90,
    "新加坡": 6,
    "马来西亚": 6,
    "日本": 40,
    "娱乐": 15,
    "体育": 20,
    "少儿": 15,
    "音乐": 8,
    "教育": 8,
    "财经": 8,
}

CHINESE_GROUPS = {
    "大陆", "中文综合", "其他地方", "中文付费", "香港", "澳门",
    "台湾", "新加坡", "马来西亚", *MAINLAND_REGION_GROUPS,
}

BLOCKED_OUTPUT_GROUPS = {"纪录片", "中文纪录", "电影", "中文电影", "新闻", "国际", "韩国"}

# Explicit English services are removed even when an upstream list files them
# under "中文综合" or a Chinese region. Word boundaries prevent short brands
# such as ABC from matching an unrelated Japanese call sign.
ENGLISH_SERVICE_RE = re.compile(
    r"(?:(?<![a-z])(?:bbc|cnn|fox\s*news|nbc(?:lx|\s*news)?|cbs\s*news|abc\s*news|"
    r"bloomberg|cnbc|reuters|sky\s*news|euronews|i24news|voa|newsmax|"
    r"cheddar\s*news|channel\s*newsasia|al\s*jazeera|france\s*24)(?![a-z])|"
    r"\bdw(?:\s+(?:english|news))?\b|nhk\s*world|kbs\s*world|arirang|"
    r"taiwan\s*(?:\+|plus)|viutv\s*six|viutvsix|tvb\s*pearl|\bpearl\b|"
    r"明珠台|英语|英語|英文|(?<![a-z])cna(?![a-z])|hoy\s+international\s+business)",
    re.I,
)

CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
REGIONAL_BRAND_RE = re.compile(
    r"\b(?:brtv|btv|cetv|jstv|gdtv|grt|tvb|rthk|hoy|viutv|"
    r"phoenix|鳳凰|凤凰|tdm|tvbs|ttv|ctv|cts|ftv|set|ebc|momo|ntd)\b",
    re.I,
)

# The living-room list is deliberately much smaller than the regional catalogue.
# catalogue. It contains only routes that survive a third fresh segment test.
# Missing groups never get padded with marginal streams merely to hit 200.
EASY_GROUP_TARGETS = {
    "卫视台": 72,
    "台湾": 20,
    "香港": 10,
    "日本": 8,
    "澳门": 4,
    "新加坡": 2,
    "北京": 4,
    "上海": 5,
    "天津": 3,
    "重庆": 3,
    "河北": 5,
    "山西": 4,
    "内蒙古": 3,
    "辽宁": 4,
    "吉林": 3,
    "黑龙江": 3,
    "江苏": 6,
    "浙江": 6,
    "安徽": 4,
    "福建": 4,
    "江西": 4,
    "山东": 5,
    "河南": 5,
    "湖北": 5,
    "湖南": 5,
    "广东": 6,
    "广西": 4,
    "海南": 3,
    "四川": 5,
    "贵州": 4,
    "云南": 4,
    "西藏": 2,
    "陕西": 4,
    "甘肃": 3,
    "青海": 2,
    "宁夏": 2,
    "新疆": 4,
    "其他地方": 12,
    "中文付费": 5,
    "体育": 5,
    "少儿": 4,
    "音乐": 3,
    "教育": 2,
}

SOURCES = [
    # Broad public directory used as the baseline.
    ("大陆", "https://iptv-org.github.io/iptv/countries/cn.m3u", False),
    ("香港", "https://iptv-org.github.io/iptv/countries/hk.m3u", True),
    ("澳门", "https://iptv-org.github.io/iptv/countries/mo.m3u", True),
    ("台湾", "https://iptv-org.github.io/iptv/countries/tw.m3u", False),
    ("新加坡", "https://iptv-org.github.io/iptv/countries/sg.m3u", True),
    ("马来西亚", "https://iptv-org.github.io/iptv/countries/my.m3u", True),
    ("日本", "https://iptv-org.github.io/iptv/countries/jp.m3u", True),
    ("娱乐", "https://iptv-org.github.io/iptv/categories/entertainment.m3u", False),
    ("音乐", "https://iptv-org.github.io/iptv/categories/music.m3u", False),
    ("体育", "https://iptv-org.github.io/iptv/categories/sports.m3u", False),
    ("少儿", "https://iptv-org.github.io/iptv/categories/kids.m3u", False),
    ("中文综合", "https://iptv-org.github.io/iptv/languages/zho.m3u", True),
    ("教育", "https://iptv-org.github.io/iptv/categories/education.m3u", False),
    ("财经", "https://iptv-org.github.io/iptv/categories/business.m3u", False),

    # Frequently refreshed Chinese IPv4 lists.  Multiple URLs for the same
    # channel are intentionally kept until *after* probing; the fastest healthy
    # variant wins instead of whichever URL happened to appear first.
    # Independent web-hosted lists discovered outside GitHub search.
    ("大陆", "https://myernestlu.github.io/zby.txt", False),
    ("大陆", "https://xxy.free.hr/YIPTV.m3u", False),
    ("大陆", "https://iptv.228088.xyz/cn.m3u", False),
    ("中文综合", "https://yang-1989.eu.org/m3u/Gather", True),
    ("大陆", "https://cdn.jsdelivr.net/gh/XiaoZhang5656/xiaozhang-5656.github.io@main/iptv-live.txt", False),
    ("大陆", "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.txt", False),
    ("大陆", "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u", False),
    ("大陆", "https://iptv.burningc4.com/TV-IPV4.m3u", False),
    ("大陆", "https://live.zbds.top/tv/iptv4.txt", False),
    ("大陆", "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/suxuang/myIPTV/main/APTV%E6%89%8B%E6%9C%BA%E4%B8%93%E4%BA%AB.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_cctv_status.m3u8", False),
    ("大陆", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_province_status.m3u8", False),
    ("中文付费", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_pay_status.m3u8", False),
    ("中文付费", "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/bestv.m3u", False),
    ("中文付费", "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/IPTV.m3u", False),
    ("中文付费", "https://raw.githubusercontent.com/imDazui/Tvlist-awesome-m3u-m3u8/master/m3u/%E8%BD%AE%E6%92%AD_%E5%8D%8E%E6%95%B0.NewTV.SiTV.CIBN.m3u", False),
    ("中文付费", "https://raw.githubusercontent.com/cjh19831115/Meroser/main/IPTV-tvbox.txt", False),
    ("中文付费", "https://raw.githubusercontent.com/haiwx/m3u8/master/%E7%94%B5%E8%A7%86%E7%9B%B4%E6%92%AD%E5%88%97%E8%A1%A8%E5%81%A5%E5%BA%B7%E7%89%88.m3u8", False),
    ("大陆", "https://m3u.ibert.me/fmml_itv.m3u", False),
    ("大陆", "https://m3u.ibert.me/fmml_index.m3u", False),
    ("大陆", "https://m3u.ibert.me/y_g.m3u", False),
    ("大陆", "https://m3u.ibert.me/cn.m3u", False),
    ("大陆", "https://m3u.ibert.me/cn_p.m3u", False),

    # An independent FTA collection supplies additional regional CDN variants.
    ("香港", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_hong_kong.m3u8", True),
    ("香港", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_hongkong.m3u8", True),
    ("澳门", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_macau.m3u8", True),
    ("台湾", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_taiwan.m3u8", True),
    ("日本", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_japan.m3u8", True),

    # Core CCTV/provincial backups. CCSH publishes many alternate HTTPS
    # variants; the builder races them and keeps only the fastest working URL.
    # Large comma-delimited Chinese pools. parse_m3u accepts both M3U and
    # name,url TXT syntax, including multiple # separated backup routes.
    ("大陆", "https://raw.githubusercontent.com/BigBigGrandG/IPTV-URL/release/Gather.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u", False),
    ("中文综合", "https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u", True),
    ("中文综合", "https://raw.githubusercontent.com/joevess/IPTV/main/home.m3u8", True),
    ("中文综合", "https://raw.githubusercontent.com/joevess/IPTV/main/iptv.m3u8", True),
    ("大陆", "https://raw.githubusercontent.com/Nekori/TV/master/A_TV.txt", False),
    ("大陆", "https://raw.githubusercontent.com/CCSH/IPTV/main/live.txt", False),
    ("大陆", "https://raw.githubusercontent.com/Supprise0901/TVBox_live/main/live.txt", False),
    ("大陆", "https://raw.githubusercontent.com/xisohi/CHINA-IPTV/main/TV/live.txt", False),
    ("大陆", "https://raw.githubusercontent.com/wwb521/live/main/tv.txt", False),
    ("大陆", "https://raw.githubusercontent.com/CCSH/IPTV/main/live_lite.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/CCSH/IPTV/main/live.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_china.m3u8", False),
    ("大陆", "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8", False),
    ("大陆", "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV1_GuoJi.m3u8", False),
    ("大陆", "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/HunanTV_AutoUpdate.m3u8", False),
    # Active, metadata-rich lists reviewed in August 2026.  Their routes still
    # go through the same manifest + media-segment probes as every other source.
    ("大陆", "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/index.m3u", False),
    ("大陆", "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/itv.m3u", False),
    ("中文综合", "https://raw.githubusercontent.com/YueChan/Live/main/GNTV.m3u", True),
    ("中文付费", "https://raw.githubusercontent.com/YueChan/Live/main/Hunan.txt", False),
    ("大陆", "https://raw.githubusercontent.com/jura00/vms/main/hd.m3u8", False),
    ("大陆", "https://raw.githubusercontent.com/JinnLynn/iptv/dist/live-ipv4.txt", False),
    # Small HTTPS relay list with currently maintained CHC linear channels.
    ("中文付费", "https://cdn.jsdelivr.net/gh/jyoketsu/tv@main/live.txt", False),
]

# Curated fallbacks. The required CCTV stations use multiple independent
# routes; blocked programme categories below are discarded before probing.
EXTRAS = [
    # China-side fallbacks for stations that are frequently unreachable from
    # GitHub's overseas runner even though current Chinese indexes carry them.
    ("CCTV-1 综合 1080p", "大陆", "https://cctvcnch5ca.v.wscdns.com/live/cctv1_2/index.m3u8?contentid=2820180516001"),
    ("CCTV-1 综合", "大陆", "http://183.196.25.171:808/hls/1/index.m3u8"),
    ("CCTV-1 综合 1080p", "大陆", "http://221.7.175.154:8445/tsfile/live/1000_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("CCTV-2 财经 1080p", "大陆", "https://cctvcnch5ca.v.wscdns.com/live/cctv2_2/index.m3u8?contentid=2820180516001"),
    ("CCTV-2 财经", "大陆", "http://183.196.25.171:808/hls/2/index.m3u8"),
    ("CCTV-2 财经 1080p", "大陆", "http://107.150.60.122/live/cctv2hd.m3u8"),
    ("CCTV-8 电视剧 1080p", "大陆", "https://cctvcnch5ca.v.wscdns.com/live/cctv8_2/index.m3u8"),
    ("CCTV-8 电视剧", "大陆", "http://183.196.25.171:808/hls/77/index.m3u8"),
    ("CCTV-8 电视剧 1080p", "大陆", "https://live.v1.mk/aishang/cctv8hd"),
    ("CCTV-11 戏曲 1080p", "大陆", "https://live.v1.mk/aishang/cctv11hd"),
    ("CCTV-11 戏曲 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqbv/cctv11hd.m3u8?auth=test20251009"),
    ("CCTV-11 戏曲 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv11hd"),
    ("CCTV-11 戏曲 1080p", "大陆", "http://120.76.248.139/live/bfgd/4200000130.m3u8"),
    # CCTV-10 quality fallbacks. Direct operator playlists often omit a
    # resolution tag, so these explicitly labelled HD siblings compete using
    # both real segment bitrate and download headroom.
    ("CCTV-10 科教 1080p", "大陆", "http://107.150.60.122/live/cctv10hd.m3u8"),
    ("CCTV-10 科教 1080p", "大陆", "http://74.91.26.218:82/live/cctv10hd.m3u8"),
    ("CCTV-10 科教 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv10hd"),
    ("CCTV-10 科教 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqbv/cctv10hd.m3u8?auth=test20251009"),
    ("CCTV-10 科教 1080p", "大陆", "http://207.56.13.146:81/cdnlive/cctv10.m3u8"),
    ("CCTV-10 科教 1080p", "大陆", "http://198.204.228.26/live/cctv10hd.m3u8"),
    ("CCTV-10 科教 1080p", "大陆", "http://204.12.221.218:8181/3m1080p/cctv10.m3u8"),
    ("CCTV-10 科教 1080p 高码", "大陆", "http://117.161.12.124/live/program/live/cctv10hd8m/8000000/mnf.m3u8"),
    # CCTV-15 alternatives on independently hosted mirrors. The previously
    # selected operator /0016_1 route is intentionally absent: it is Guangdong
    # Satellite TV on the family's actual television.
    ("CCTV-15 音乐 1080p", "大陆", "http://107.150.60.122/live/cctv15hd.m3u8"),
    ("CCTV-15 音乐 1080p", "大陆", "http://74.91.26.218:82/live/cctv15hd.m3u8"),
    ("CCTV-15 音乐 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv15hd"),
    ("CCTV-15 音乐 1080p", "大陆", "http://198.204.228.26/live/cctv15hd.m3u8"),
    ("CCTV-15 音乐 1080p", "大陆", "http://207.56.13.146:81/cdnlive/cctv15.m3u8"),
    ("CCTV-15 音乐 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqbv/cctv15hd.m3u8?auth=test20251009"),
    ("CCTV-15 音乐 1080p", "大陆", "http://113.57.140.161:10081/newlive/live/hls/16/live.m3u8"),
    ("CCTV-15 音乐 1080p", "大陆", "http://58.56.162.102:4466/newlive/live/hls/16/live.m3u8"),
    ("CCTV-15 音乐 1080p", "大陆", "http://219.147.245.238:25480/newlive/live/hls/16/live.m3u8"),
    ("CCTV-15 音乐", "大陆", "http://183.196.25.171:808/hls/15/index.m3u8"),
    ("CCTV-15 音乐", "大陆", "http://hwrr.jx.chinamobile.com:8080/PLTV/88888888/224/3221225641/index.m3u8"),
    # CCTV-5 must remain 1080p. These are video-CDN paths (not the AAC-only
    # /audio/ feed that previously produced a black screen in APTV).
    ("CCTV-5 体育 1080p 海外镜像", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub"),
    ("CCTV-5 体育 1080p 实测", "大陆", "http://198.204.228.26/live/cctv5hd.m3u8"),
    ("CCTV-5 体育 720p 备用", "大陆", "http://207.56.13.146:81/cdnlive/cctv5.m3u8"),
    ("CCTV-5 体育 1080p 高码", "大陆", "http://117.161.12.124/live/program/live/cctv5hd8m/8000000/mnf.m3u8"),
    ("CCTV-5 体育 1080p 百视通", "大陆", "http://120.76.248.139/live/bfgd/4200000064.m3u8"),
    ("CCTV-5 体育 1080p 教育网镜像", "大陆", "http://video.qd.sdu.edu.cn/liverespath/3f76badfb3a23d95f26ff573a93902bbdb8b8e98/index.m3u8"),
    ("CCTV-5 体育 1080p 动态CDN", "大陆", "https://live.goodiptv.club/api/bestv.php?id=cctv5hd8m/8000000"),
    ("CCTV-5 体育 1080p 百视通镜像", "大陆", "http://101.33.17.11/liveplay-kk.rtxapp.com/live/program/live/cctv5hd8m/8000000/mnf.m3u8"),
    ("CCTV-5 体育 1080p 百视通镜像", "大陆", "http://180.97.247.27:8088/liveplay-kk.rtxapp.com/live/program/live/cctv5hd8m/8000000/mnf.m3u8"),
    ("CCTV-5 体育 1080p CDN", "大陆", "https://cctvalih5ca.v.myalicdn.com/live/cctv5_2/index.m3u8?contentid=2820180516001"),
    ("CCTV-5 体育 1080p CDN", "大陆", "https://cctvcnch5ca.v.wscdns.com/live/cctv5_2/index.m3u8?contentid=2820180516001"),
    ("CCTV-5 体育 1080p CDN", "大陆", "http://cctvalih5ca.v.myalicdn.com/live/cctv5_2/index.m3u8"),
    # The adaptive Kua route tops out at 720p and is retained only as source
    # research; the 1080p-only publishing rule below prevents it becoming main.
    ("CCTV-5 体育 720p 自适应 CDN", "大陆", "https://ldcctvwbcdks.v.kcdnvip.com/ldcctvwbcd/cdrmldcctv5_1/index.m3u8?b=200-2100"),
    # Operator/regional routes are last-resort alternatives only. They can win
    # a short GitHub probe while remaining slow across the user's actual ISP.
    ("CCTV-5 体育 1080p", "大陆", "http://dbiptv.sn.chinamobile.com/PLTV/88888890/224/3221226395/index.m3u8"),
    ("CCTV-5 体育 1080p", "大陆", "http://39.134.24.161/dbiptv.sn.chinamobile.com/PLTV/88888890/224/3221226395/index.m3u8"),
    ("CCTV-5 体育 1080p", "大陆", "http://39.134.24.162/dbiptv.sn.chinamobile.com/PLTV/88888888/224/3221226395/1.m3u8"),
    ("CCTV-5 体育 1080p", "大陆", "http://120.196.232.31:8088/rrs03.hw.gmcc.net/PLTV/651/224/3221226731/1.m3u8"),
    ("CCTV-5 体育 1080p", "大陆", "http://39.134.66.110/PLTV/88888888/224/3221225818/index.m3u8"),
    ("湖南卫视 2160p", "大陆", "http://120.196.232.43:8088/rrs03.hw.gmcc.net/PLTV/651/224/3221226698/1.m3u8"),
    ("湖南卫视 2160p", "大陆", "http://hlsal-ldvt.qing.mgtv.com/nn_live/nn_x64/aWQ9SE5XU1pHU1Qmcz0yNDAwJmQ9OTkmaHNpemU9MzIwMDAwMDAw/n_index.m3u8"),
    ("湖南卫视 1080p", "大陆", "http://221.7.175.154:8445/tsfile/live/0128_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("山东卫视 1080p", "大陆", "http://183.251.61.207/PLTV/88888888/224/3221225843/index.m3u8"),
    ("山东卫视 1080p", "大陆", "http://107.150.60.122/live/sdwshd.m3u8"),
    # Shanxi Satellite TV was the sole missing provincial service in the
    # August 2026 family-list audit. Keep independent IPv4 plus native-IPv6
    # candidates; only routes that pass the normal live-segment checks publish.
    ("山西卫视 1080p", "大陆", "http://115.225.31.114:9901/tsfile/live/0118_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("山西卫视 1080p", "大陆", "http://101.66.198.179:9901/tsfile/live/0118_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("山西卫视 1080p", "大陆", "http://153.0.171.163:9901/tsfile/live/0118_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("山西卫视 1080p", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225624/index.m3u8"),
    ("山西卫视 540p", "大陆", "http://39.135.253.53/hwcdntest.hb.chinamobile.com/PLTV/88888888/224/3221226174/1.m3u8"),
    ("山西卫视 540p", "大陆", "http://39.135.253.53/hwcdntest.hb.chinamobile.com/PLTV/88888888/224/3221226174/2.m3u8"),
    ("山西卫视 1080p IPv6", "大陆", "http://[2409:8087:74d9:21::6]/270000001128/9900000053/index.m3u8"),
    ("山西卫视 720p IPv6", "大陆", "http://[2409:8087:74d9:21::6]/000000001000PLTV/88888888/224/3221226543/index.m3u8"),
    ("山西卫视 720p IPv6", "大陆", "http://[2409:8087:74d9:21::6]/270000001322/69900158041111100000002214/index.m3u8"),
    ("山西卫视 1080p", "大陆", "http://198.204.228.26/live/sxwshd.m3u8"),
    ("山西卫视 1080p", "大陆", "http://204.12.221.218:8181/3m1080p/sxws.m3u8"),
    ("山西卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=sxwshd"),
    ("山西卫视 1080p", "大陆", "http://119.39.9.8:9901/tsfile/live/0118_1.m3u8"),
    # Replacements for satellite routes removed by the visual frame audit.
    # These mirrors are independent candidates; none is published unless it
    # survives the normal repeated manifest and media-segment probes.
    ("北京卫视 1080p", "大陆", "http://198.204.228.26/live/bjwshd.m3u8"),
    ("北京卫视 1080p", "大陆", "http://107.150.60.122/live/bjwshd.m3u8"),
    ("北京卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=bjwshd"),
    ("内蒙古卫视 1080p", "大陆", "http://198.204.228.26/live/nmgws.m3u8"),
    ("内蒙古卫视 1080p", "大陆", "http://107.150.60.122/live/nmgws.m3u8"),
    ("内蒙古卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=nmgws"),
    ("内蒙古卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/nmgws.m3u8?auth=testpub"),
    ("内蒙古卫视 1080p", "大陆", "http://61.178.227.57:9901/tsfile/live/0109_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("内蒙古卫视 1080p", "大陆", "http://120.238.84.45:9901/tsfile/live/1072_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("新疆卫视 1080p", "大陆", "http://198.204.228.26/live/xjws.m3u8"),
    ("新疆卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=xjws"),
    ("新疆卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/xjws.m3u8?auth=testpub"),
    ("新疆卫视 1080p", "大陆", "http://113.25.252.226:9901/tsfile/live/1044_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("新疆卫视 1080p", "大陆", "http://58.57.40.22:9901/tsfile/live/1055_1.m3u8"),
    ("新疆卫视 1080p", "大陆", "http://36.32.174.67:60080/newlive/live/hls/47/live.m3u8"),
    ("青海卫视 1080p", "大陆", "http://198.204.228.26/live/qhws.m3u8"),
    ("青海卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=qhws"),
    ("青海卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/qhws.m3u8?auth=testpub"),
    ("青海卫视 1080p", "大陆", "http://204.12.221.218:8181/3m1080p/qhws.m3u8"),
    ("青海卫视 1080p", "大陆", "http://61.178.227.57:9901/tsfile/live/0140_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("青海卫视 1080p", "大陆", "http://112.27.5.218:9901/tsfile/live/0140_2.m3u8?key=txiptv&playlive=1&authid=0"),
    # The poisoned multi-label relay had been masking weak coverage for these
    # six satellites. Use explicit station IDs on several unrelated mirrors.
    ("云南卫视 1080p", "大陆", "http://198.204.228.26/live/ynws.m3u8"),
    ("云南卫视 1080p", "大陆", "http://107.150.60.122/live/ynws.m3u8"),
    ("云南卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=ynws"),
    ("云南卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/ynws.m3u8?auth=testpub"),
    ("云南卫视 1080p", "大陆", "http://204.12.221.218:8181/3m1080p/ynws.m3u8"),
    ("陕西卫视 1080p", "大陆", "http://198.204.228.26/live/shxws.m3u8"),
    ("陕西卫视 1080p", "大陆", "http://107.150.60.122/live/shxws.m3u8"),
    ("陕西卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=shxws"),
    ("陕西卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/shxws.m3u8?auth=testpub"),
    ("吉林卫视 1080p", "大陆", "http://198.204.228.26/live/jlws.m3u8"),
    ("吉林卫视 1080p", "大陆", "http://107.150.60.122/live/jlws.m3u8"),
    ("吉林卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=jlws"),
    ("吉林卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/jlws.m3u8?auth=testpub"),
    ("甘肃卫视 1080p", "大陆", "http://198.204.228.26/live/gsws.m3u8"),
    ("甘肃卫视 1080p", "大陆", "http://107.150.60.122/live/gsws.m3u8"),
    ("甘肃卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=gsws"),
    ("甘肃卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/gsws.m3u8?auth=testpub"),
    ("宁夏卫视 1080p", "大陆", "http://198.204.228.26/live/nxws.m3u8"),
    ("宁夏卫视 1080p", "大陆", "http://107.150.60.122/live/nxws.m3u8"),
    ("宁夏卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=nxws"),
    ("宁夏卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/nxws.m3u8?auth=testpub"),
    ("海南卫视 1080p", "大陆", "http://198.204.228.26/live/lyws.m3u8"),
    ("海南卫视 1080p", "大陆", "http://107.150.60.122/live/lyws.m3u8"),
    ("海南卫视 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=lyws"),
    ("海南卫视 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/lyws.m3u8?auth=testpub"),
    # Fresh operator-address candidates from the 2026-08-21 IPv4 catalogue.
    ("湖北卫视 1080p", "大陆", "http://39.134.67.108/PLTV/88888888/224/3221225975/1.m3u8"),
    ("湖北卫视 1080p", "大陆", "http://39.134.65.162/PLTV/88888888/224/3221225569/1.m3u8"),
    ("湖北卫视 1080p", "大陆", "http://14.29.76.30:9901/tsfile/live/0132_1.m3u8?key=txiptv"),
    ("陕西卫视 1080p", "大陆", "http://39.164.160.249:9901/tsfile/live/0136_1.m3u8"),
    ("陕西卫视 576p", "大陆", "http://39.134.144.6:8089/PLTV/88888888/224/3221226035/index.m3u8"),
    ("陕西卫视 1080p", "大陆", "http://stream.snrtv.com/sxtvs-star.m3u8"),
    ("吉林卫视 1080p", "大陆", "http://39.134.67.108/PLTV/88888888/224/3221226013/1.m3u8"),
    ("吉林卫视 1080p", "大陆", "http://117.27.190.42:9998/tsfile/live/23258_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("吉林卫视 1080p", "大陆", "http://175.18.189.238:9902/tsfile/live/0116_1.m3u8"),
    ("甘肃卫视 1080p", "大陆", "http://live.nctv.top/886/sxg.php?id=gsws_4000"),
    ("甘肃卫视 1080p", "大陆", "http://14.29.76.30:9901/tsfile/live/0141_1.m3u8?key=txiptv"),
    ("甘肃卫视 1080p", "大陆", "http://218.13.170.98:9901/tsfile/live/0141_1.m3u8"),
    ("甘肃卫视 1080p", "大陆", "http://222.240.82.92:9901/tsfile/live/0141_1.m3u8"),
    ("海南卫视 1080p", "大陆", "http://39.134.67.108/PLTV/88888888/224/3221226026/1.m3u8"),
    ("海南卫视 1080p", "大陆", "http://119.62.36.174:9901/tsfile/live/1003_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("海南卫视 1080p", "大陆", "http://117.27.190.42:9998/tsfile/live/23273_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("山东卫视 1080p", "大陆", "http://204.12.221.218:8181/3m1080p/sdws.m3u8"),
    ("CCTV-5 体育", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221226019/index.m3u8"),
    ("CCTV-5 体育", "大陆", "http://newvideo.dangtutv.cn:8278/CCTVsports/playlist.m3u8"),
    ("CCTV-5 体育", "大陆", "http://140.207.241.2:8080/live/program/live/cctv5hd/4000000/mnf.m3u8"),
    ("CCTV-5+ 体育赛事", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225603/index.m3u8"),
    ("CCTV-5+ 体育赛事", "大陆", "http://39.135.140.227:6610/PLTV/88888888/224/3221225649/2/index.m3u8?fmt=ts2hls"),
    ("CCTV-5+ 体育赛事", "大陆", "http://69.30.246.194/live/cctv5p.m3u8"),
    ("CCTV-9 纪录", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225734/index.m3u8"),
    ("CCTV-9 纪录", "大陆", "http://125.210.152.18:9090/live/CCTVJLHD_H265.m3u8"),
    ("CCTV-9 纪录", "大陆", "http://204.12.221.218:8181/3m1080p/cctv9.m3u8"),
    ("CCTV-12 社会与法", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225731/index.m3u8"),
    ("CCTV-12 社会与法", "大陆", "https://cctvalih5ca.v.myalicdn.com/live/cctv12_2/index.m3u8?contentid=2820180516001"),
    ("CCTV-12 社会与法 1080p", "大陆", "http://218.98.16.2:8088/live.hcs.cmvideo.cn:8088/migu/kailu/20200324/cctv12hd/57/index.m3u8?&encrypt=1"),
    ("CCTV-12 社会与法 1080p", "大陆", "http://live.hcs.cmvideo.cn:8088/migu/kailu/20200324/cctv12hd/57/index.m3u8?&encrypt=1"),
    ("CCTV-16 奥林匹克", "大陆", "http://liveop.cctv.cn/hls/CCTV16HD/playlist.m3u8"),
    ("CCTV-16 奥林匹克", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221226100/index.m3u8"),
    ("CCTV-16 奥林匹克", "大陆", "https://epg.pw/stream/c1beb2abcba5aef09c2f58efc3ca84b76de2c7b9cf60762b0d79772d9e70d454.m3u8"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://107.150.60.122/live/cctv16hd.m3u8"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://198.204.228.26/live/cctv16hd.m3u8"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://74.91.26.218:82/live/cctv16hd.m3u8"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv16hd"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/cctv16hd.m3u8?auth=testpub"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://204.12.221.218:8181/3m1080p/cctv16.m3u8"),
    ("CCTV-16 奥林匹克 1080p", "大陆", "http://69.30.246.194/live/cctv16hd.m3u8"),
    # User-route replacements for an expiring signed CCTV-4K URL and a
    # buffering CCTV-15 GitHub Pages relay. Every candidate still must pass
    # two live-manifest and media-segment probes before publication.
    ("CCTV-4K 超高清", "大陆", "http://27.222.3.214/liveali-tp4k.cctv.cn/live/4K10M.stream/playlist.m3u8"),
    ("CCTV-4K 超高清", "大陆", "http://101.35.240.114:88/live.php?id=CCTV4K"),
    ("CCTV-4K 超高清", "大陆", "http://116.52.173.151:1234/624878970"),
    ("CCTV-15 音乐", "大陆", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225601/index.m3u8"),
    ("CCTV-15 音乐", "大陆", "http://61.136.172.236:9901/tsfile/live/0015_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("CCTV-15 音乐", "大陆", "http://113.57.140.161:10081/newlive/live/hls/16/live.m3u8"),
    ("求索纪录", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000010.m3u8?cdn=hnbblive"),
    ("求索科学", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000011.m3u8?cdn=hnbblive"),
    ("求索生活", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000008.m3u8?cdn=hnbblive"),
    ("求索动物", "中文纪录", "http://home.wwang.pw:35455/itv/2000000004000000009.m3u8?cdn=hnbblive"),
    ("CHC影迷电影", "中文电影", "http://58.19.38.162:9901/tsfile/live/1004_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("CHC动作电影", "中文电影", "http://58.19.38.162:9901/tsfile/live/1005_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("CHC家庭影院", "中文电影", "http://58.19.38.162:9901/tsfile/live/1006_1.m3u8?key=txiptv&playlive=1&authid=0"),
    ("纪实科教8K", "中文纪录", "http://111.31.106.140/downflv.brtvcloud.com/8klive/8kliveok.m3u8"),
    ("求索纪录 1080p", "中文纪录", "http://39.134.66.66/PLTV/88888888/224/3221225713/index.m3u8"),
    ("求索科学 1080p", "中文纪录", "http://39.134.66.66/PLTV/88888888/224/3221225728/index.m3u8"),
    ("求索生活 1080p", "中文纪录", "http://39.134.66.66/PLTV/88888888/224/3221225715/index.m3u8"),
    ("CHC高清电影 1080p", "中文电影", "http://39.134.19.252:6610/yinhe/2/ch00000090990000002065/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC家庭影院 1080p", "中文电影", "http://39.134.19.252:6610/yinhe/2/ch00000090990000002085/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC动作电影 1080p", "中文电影", "http://111.20.33.93/PLTV/88888893/224/3221226465/index.m3u8"),
    ("NewTV超级电影 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225717/index.m3u8"),
    ("NewTV超级电视剧 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225716/index.m3u8"),
    ("NewTV东北热剧 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225741/index.m3u8"),
    ("NewTV欢乐剧场 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225742/index.m3u8"),
    ("NewTV超级体育 1080p", "中文付费", "http://111.13.111.242/otttv.bj.chinamobile.com/PLTV/88888888/224/3221226232/1.m3u8"),

    # Premium-channel alternatives collected from independent public indexes.
    # Curated means every route is probed; only a real live HLS manifest plus a
    # downloadable media segment can enter tv.m3u.
    ("求索动物 1080p", "中文纪录", "http://39.134.66.66/PLTV/88888888/224/3221225730/index.m3u8"),
    ("求索动物 1080p", "中文纪录", "http://39.134.134.87/000000001000/6000000002000010046/index.m3u8"),
    ("求索纪录 1080p", "中文纪录", "http://39.134.134.87/000000001000/6000000002000032052/index.m3u8"),
    ("求索科学 1080p", "中文纪录", "http://39.134.134.87/000000001000/6000000002000032344/index.m3u8"),
    ("求索生活 1080p", "中文纪录", "http://39.134.134.87/000000001000/6000000002000003382/index.m3u8"),

    ("CHC动作电影 1080p", "中文电影", "https://live.v1.mk/aishang/chcdzdy"),
    ("CHC高清电影 1080p", "中文电影", "https://live.v1.mk/aishang/chcgqdy"),
    ("CHC家庭影院 1080p", "中文电影", "https://live.v1.mk/aishang/chcjtyy"),
    ("CHC动作电影 1080p", "中文电影", "http://38.75.136.137:98/gslb/dsdqpub/chcdz.m3u8?auth=testpub"),
    ("CHC动作电影 1080p", "中文电影", "http://198.204.228.26/live/chcdz.m3u8"),
    ("CHC家庭影院 1080p", "中文电影", "http://38.75.136.137:98/gslb/dsdqpub/chcjt.m3u8?auth=testpub"),
    ("CHC家庭影院 1080p", "中文电影", "http://198.204.228.26/live/chcjt.m3u8"),
    ("CHC影迷电影 1080p", "中文电影", "http://38.75.136.137:98/gslb/dsdqpub/chchd.m3u8?auth=testpub"),

    ("第一剧场 1080p", "中文付费", "http://38.75.136.137:98/gslb/dsdqpub/dyjc.m3u8?auth=testpub"),
    ("第一剧场 720p", "中文付费", "http://173.208.212.130:8181/720p/dyjc.m3u8"),
    ("第一剧场 1080p", "中文付费", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=dyjc"),
    ("世界地理 1080p", "中文付费", "http://38.75.136.137:98/gslb/dsdqpub/sjdl.m3u8?auth=testpub"),
    ("世界地理 1080p", "中文付费", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=sjdl"),
    ("风云剧场 1080p", "中文付费", "http://38.75.136.137:98/gslb/dsdqpub/fyjc.m3u8?auth=testpub"),
    ("风云剧场 720p", "中文付费", "http://173.208.212.130:8181/720p/fyjc.m3u8"),
    ("风云剧场 1080p", "中文付费", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=fyjc"),
    ("风云足球 1080p", "中文付费", "http://38.75.136.137:98/gslb/dsdqpub/fyzq.m3u8?auth=testpub"),
    ("风云足球 1080p", "中文付费", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=fyzq"),
    ("风云音乐 1080p", "中文付费", "http://38.75.136.137:98/gslb/dsdqpub/fyyy.m3u8?auth=testpub"),
    ("风云音乐 1080p", "中文付费", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=fyyy"),

    # Complete NewTV family: two current China Mobile route families are raced.
    ("NewTV中国功夫 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225681/index.m3u8"),
    ("NewTV军事评论 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225668/index.m3u8"),
    ("NewTV军旅剧场 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225676/index.m3u8"),
    ("NewTV农业致富 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225683/index.m3u8"),
    ("NewTV动作电影 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225661/index.m3u8"),
    ("NewTV古装剧场 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225663/index.m3u8"),
    ("NewTV家庭剧场 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225677/index.m3u8"),
    ("NewTV怡伴健康 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225673/index.m3u8"),
    ("NewTV惊悚悬疑 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225665/index.m3u8"),
    ("NewTV明星大片 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225664/index.m3u8"),
    ("NewTV武搏世界 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225680/index.m3u8"),
    ("NewTV海外剧场 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225671/index.m3u8"),
    ("NewTV潮妈辣婆 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225685/index.m3u8"),
    ("NewTV炫舞未来 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225719/index.m3u8"),
    ("NewTV爱情喜剧 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225669/index.m3u8"),
    ("NewTV精品体育 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225674/index.m3u8"),
    ("NewTV精品大剧 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225670/index.m3u8"),
    ("NewTV精品纪录 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225672/index.m3u8"),
    ("NewTV精品萌宠 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221226508/index.m3u8"),
    ("NewTV超级综艺 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225714/index.m3u8"),
    ("NewTV金牌综艺 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225666/index.m3u8"),
    ("NewTV黑莓动画 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225662/index.m3u8"),
    ("NewTV黑莓电影 1080p", "中文付费", "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225743/index.m3u8"),

    ("NewTV爱情喜剧 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225533/index.m3u8"),
    ("NewTV超级电视剧 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225637/index.m3u8"),
    ("NewTV超级电影 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225644/index.m3u8"),
    ("NewTV超级体育 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225635/index.m3u8"),
    ("NewTV超级综艺 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225642/index.m3u8"),
    ("NewTV潮妈辣婆 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225542/index.m3u8"),
    ("NewTV东北热剧 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225679/index.m3u8"),
    ("NewTV欢乐剧场 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225682/index.m3u8"),
    ("NewTV金牌综艺 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225525/index.m3u8"),
    ("NewTV惊悚悬疑 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225553/index.m3u8"),
    ("NewTV精品大剧 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225536/index.m3u8"),
    ("NewTV精品纪录 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225545/index.m3u8"),
    ("NewTV精品体育 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225526/index.m3u8"),
    ("NewTV农业致富 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225552/index.m3u8"),
    ("NewTV中国功夫 1080p", "中文付费", "http://39.134.65.162/PLTV/88888888/224/3221225604/index.m3u8"),

    # A second independent pass: China Telecom/Shaanxi and CQCCN routes.
    ("CHC动作电影 1080p", "中文电影", "http://39.135.89.3:6610/yinhe/2/ch00000090990000002055/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC家庭影院 1080p", "中文电影", "http://39.135.89.3:6610/yinhe/2/ch00000090990000002085/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC高清电影 1080p", "中文电影", "http://39.135.89.3:6610/yinhe/2/ch00000090990000002065/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC高清电影 1080p", "中文电影", "http://111.20.105.60:6060/yinhe/2/ch00000090990000002065/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC家庭影院 1080p", "中文电影", "http://111.20.105.60:6060/yinhe/2/ch00000090990000002085/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC动作电影 1080p", "中文电影", "http://111.20.105.60:6060/yinhe/2/ch00000090990000002055/index.m3u8?virtualDomain=yinhe.live_hls.zte.com"),
    ("CHC高清电影 1080p", "中文电影", "http://baidu.live.cqccn.com/__cl/cg:live/__c/chcgqdyHD/__op/default/__f//index.m3u8"),

    ("第一剧场 1080p", "中文付费", "http://baidu.live.cqccn.com/__cl/cg:live/__c/diyijuchangHD/__op/default/__f//index.m3u8"),
    ("风云剧场 1080p", "中文付费", "http://baidu.live.cqccn.com/__cl/cg:live/__c/fyjcHD/__op/default/__f//index.m3u8"),
    ("风云音乐 1080p", "中文付费", "http://baidu.live.cqccn.com/__cl/cg:live/__c/fyyyHD/__op/default/__f//index.m3u8"),
    ("风云足球 1080p", "中文付费", "http://baidu.live.cqccn.com/__cl/cg:live/__c/fyzqHD/__op/default/__f//index.m3u8"),
    ("风云足球 1080p", "中文付费", "http://111.20.40.170/PLTV/88888893/224/3221226984/index.m3u8"),
    ("风云足球 1080p", "中文付费", "http://220.177.175.45/tlivectfree-cdn.ysp.cctv.cn/001/2012514203.m3u8"),
    ("风云足球 1080p", "中文付费", "http://42.176.185.28:9901/tsfile/live/1017_1.m3u8"),
    ("世界地理 1080p", "中文付费", "http://baidu.live.cqccn.com/__cl/cg:live/__c/sjdlHD/__op/default/__f//index.m3u8"),
    ("世界地理 1080p", "中文付费", "http://baidu.live.cqccn.com/__cl/cg:live/__c/shijiediliHD/__op/default/__f//index.m3u8"),

    ("求索纪录 1080p", "中文纪录", "http://baidu.live.cqccn.com/__cl/cg:live/__c/qsjlHD/__op/default/__f//index.m3u8"),
    ("求索动物 1080p", "中文纪录", "http://baidu.live.cqccn.com/__cl/cg:live/__c/qsdwHD/__op/default/__f//index.m3u8"),
    ("求索科学 1080p", "中文纪录", "http://baidu.live.cqccn.com/__cl/cg:live/__c/qskxHD/__op/default/__f//index.m3u8"),
    ("求索生活 1080p", "中文纪录", "http://baidu.live.cqccn.com/__cl/cg:live/__c/qsshHD/__op/default/__f//index.m3u8"),
]

BLOCK_WORDS = [
    "shopping", "shop ", " shop", "qvc", "hsn", "jewelry", "jewellery",
    "teleshop", "home shopping", "shop lc", "gemporia", "购物", "購物", "珠宝", "珠寶", "导购", "導購",
    "免费订阅", "请勿贩卖", "维护时间", "维护内容", "公告说明", "使用说明",
    "douyu", "斗鱼", "虎牙直播",
]
LOW_VALUE = ["radio", "weather", "天气", "氣象", "parliament", "council", "assembly", "legislature"]

# Public Chinese lists sometimes mix individual films/episodes into the live-TV
# section.  Real stations normally carry explicit tvg metadata or a clear
# station/service marker; plain programme titles carry neither.
STATION_NAME_HINTS = (
    "cctv", "cgtn", "央视", "卫视", "频道", "电视", "tv", "news", "新闻",
    "卡通", "少儿", "纪录", "电影", "影院", "剧场", "动漫", "体育", "足球",
    "篮球", "网球", "高尔夫", "台球", "戏曲", "音乐", "财经", "法治", "公共",
    "都市", "综合", "经济", "生活", "科教", "教育", "文化", "国际", "中文",
    "资讯", "影视", "娱乐", "综艺", "农业", "农林", "农民", "健康", "凤凰",
    "chc", "newtv", "sitv", "ihot", "龙华", "翡翠", "无线", "明珠", "cnn",
    "voa", "4k", "8k", "cetv", "brtv", "btv", "jstv", "gdtv",
)
NON_TELEVISION_HINTS = ("广播电台", "电台", "广播", "之声", "radio", " fm ", "fm-")
PROGRAM_TITLE_RE = re.compile(
    r"(?:第[一二三四五六七八九十百\d]+[季集期部]|[（(](?:上|下|完|\d+)[）)]$|(?:上|下)$)",
    re.I,
)
CLEAR_STATION_RE = re.compile(
    r"(?:cctv|cgtn|卫视|频道|电视|\btv\b|news|newtv|sitv|cibn|chc|求索|剧场|影院)",
    re.I,
)
PUBLIC_PAY_HINTS = (
    "求索", "chc", "newtv", "sitv", "ihot", "cibn", "百视通", "华数",
    "第一剧场", "怀旧剧场", "风云足球", "风云剧场", "世界地理", "女性时尚",
    "电视指南", "家庭影院", "动作电影", "超级电影", "超级电视剧", "精品大剧",
)
PREFERRED_HOSTS = [
    "akamaized.net", "akamaihd.net", "cloudfront.net", "alicdn.com", "myalicdn.com",
    "brtvcloud.com", "fastly", "cloudflare", "brightcove", "bcovlive", "rthk", "hoy.tv",
    "streamingfast.net", "cdn", "edge",
]
CCTV5_EDGE_HOSTS = ("myalicdn.com", "wscdns.com")
CCTV5_PREFERRED_1080_URLS = (
    "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub",
    "http://198.204.228.26/live/cctv5hd.m3u8",
)
CCTV5_OPERATOR_HINTS = ("chinamobile.com", "gmcc.net", "cmvideo.cn", "gitv.tv")
UNSTABLE_HOST_HINTS = [
    "zzy", "wwang", "qqff", "7766.org", "8866.org", "3322.org", "vicp.net",
    ".xyz", ".top", ".pw", ".work", ".icu",
]
# These relays can return a valid HLS stream containing a static "signal
# interrupted" slate. Segment downloads alone therefore produce false health.
PLACEHOLDER_RELAY_HOSTS = {"t.freetv.fun", "epg.pw"}
# Public indexes occasionally attach a valid unrelated station to the wrong
# Chinese label. This URL is Tu Canal Musical, not NewTV Super Movie.
MISLABELLED_STREAM_URLS = {
    "https://cloudvideo.servers10.com:8081/8130/index.m3u8",
    "http://antvlive.ab5c6921.cdnviet.com/antv/playlist.m3u8",
    # User-tested: this Hebei TV event route currently returns a VU Networks
    # calibration/test card, although public indexes label it as CCTV-13.
    "https://event.pull.hebtv.com/jishi/cp1.m3u8",
    "https://event.pull.hebtv.com/jishi/cp2.m3u8",
    # User-tested: public indexes label this redirect as CCTV-4K, but its
    # actual programme is TVB Jade (翡翠台).
    "http://r.jdshipin.com/krMB5",
    # Public lists label this Zhejiang Shaoxing/Shengzhou local feed as
    # Shandong Satellite TV. Keep it out even when its HLS probe succeeds.
    "http://l.cztvcloud.com/channels/lantian/SXshengzhou1/720p.m3u8",
    # User-tested: valid H.264 video but sustained throughput is too unstable
    # for CCTV-5, despite passing short remote probes.
    "http://gmxw.7766.org:808/hls/93/index.m3u8",
    # User-confirmed on the living-room television: this operator route is
    # Guangdong Satellite TV, although public indexes label it as CCTV-15.
    "http://112.123.243.37:50085/tsfile/live/0016_1.m3u8?key=txiptv&playlive=0&authid=0",
    # Frame audit 2026-08-25: this Guangdong route displays a permanent source
    # warning card instead of the live satellite programme.
    "https://h5cul1yar48um3t.wcetv.com/hls/gdsatellite.m3u8",
    # Frame audit 2026-08-25: public indexes call this Inner Mongolia, but the
    # captured programme and logo are Zhejiang/China Blue News.
    "https://ali-m-l.cztv.com/channels/lantian/channel007/1080p.m3u8",
    # Frame audit 2026-08-25: this Guangxi route decodes into a full-screen
    # vertical-stripe corruption frame even though HLS segment reads pass.
    "https://hlscdn.liangtv.cn/live/0c4ef3a44b934cacb8b47121dfada66c/d7e04258157b480dae53883cc6f8123b.m3u8",
}
USER_REJECTED_ROUTE_PREFIXES = (
    # User-tested on the actual APTV route: signed CCTV 4K URLs expire or fail
    # even after a fast GitHub probe; the xykt CCTV-15 route buffers heavily.
    "https://live-play-hls.cctvnews.cctv.com/CCTVChannel/channel_cctv4k_",
    "https://xykt-fix.github.io/play/a02e/",
    # Frame audit 2026-08-25: these relay families returned CCTV-1 under many
    # different satellite labels (and a suspicious Beijing/Qinghai feed).
    "http://t.061899.xyz/",
    "http://cdn6.bkpcp.top/",
    "http://go.bkpcp.top/",
)
USER_PREFERRED_ROUTE_PREFIXES = (
    "http://27.222.3.214/liveali-tp4k.cctv.cn/live/4K10M.stream/playlist.m3u8",
    "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225601/index.m3u8",
)
REQUIRED_CORE_IDS = ("cctv5", "cctv5plus", "cctv9", "cctv12", "cctv16")
NUMBERED_CCTV_IDS = tuple(f"cctv{number}" for number in range(1, 18))
CCTV_AUDIT_IDS = NUMBERED_CCTV_IDS + ("cctv4k", "cctv5plus")
CHINA_SIDE_FALLBACK_IDS = {"cctv1", "cctv2", "cctv5", "cctv8", "cctv11", "湖南卫视", "山东卫视"}

MAINLAND_SATELLITE_NAMES = (
    "北京卫视", "东方卫视", "湖南卫视", "浙江卫视", "江苏卫视", "广东卫视", "深圳卫视",
    "安徽卫视", "山东卫视", "河南卫视", "湖北卫视", "辽宁卫视", "黑龙江卫视", "四川卫视",
    "重庆卫视", "天津卫视", "河北卫视", "江西卫视", "广西卫视", "贵州卫视", "云南卫视",
    "陕西卫视", "山西卫视", "吉林卫视", "内蒙古卫视", "新疆卫视", "西藏卫视", "青海卫视",
    "甘肃卫视", "宁夏卫视", "海南卫视", "东南卫视", "延边卫视", "海峡卫视", "兵团卫视",
    "安多卫视", "农林卫视", "三沙卫视",
)
# The exact Shanxi route below was decoded and visually confirmed as 山西卫视.
# Domestic operator paths occasionally download slowly from the overseas
# Actions runner, so it may use a 0.35 Mbps *download* floor after two fresh
# reads. Its programme bitrate and picture remain independently measured.
VISUALLY_CONFIRMED_CORE_URLS = {
    "http://153.0.171.163:9901/tsfile/live/0118_1.m3u8?key=txiptv&playlive=1&authid=0",
}
# These domestic routes have repeatedly delivered two full media segments but
# appear slow only from GitHub's overseas runner. They may reach the temporary
# family fallback at 0.35 Mbps download speed so the subsequent frame audit
# can verify their picture; one-shot routes and failed segments remain barred.
DOMESTIC_FRAME_AUDIT_CORE_URLS = {
    "http://120.198.95.220:9901/tsfile/live/1056_1.m3u8?key=txiptv&playlive=1&down=1",
    "http://112.123.243.37:50085/tsfile/live/1001_1.m3u8?key=txiptv&playlive=0&authid=0",
    "https://liveout.xntv.tv/a65jur/96iln2.m3u8",
}
MAINLAND_SATELLITE_CHINESE_ALIASES = {
    "上海卫视": "东方卫视",
    "内蒙卫视": "内蒙古卫视",
    "旅游卫视": "海南卫视",
}
MAINLAND_SATELLITE_ENGLISH_ALIASES = {
    "beijingsatellitetv": "北京卫视", "beijingtv": "北京卫视",
    "dragontv": "东方卫视", "shanghaitv": "东方卫视",
    "hunantv": "湖南卫视", "zhejiangtv": "浙江卫视",
    "jiangsusatellitetv": "江苏卫视", "jiangsutv": "江苏卫视",
    "guangdongsatellitetv": "广东卫视", "guangdongtv": "广东卫视",
    "shenzhensatellitetv": "深圳卫视", "shenzhentv": "深圳卫视",
    "anhuitv": "安徽卫视", "shandongtv": "山东卫视", "henantv": "河南卫视",
    "hubeitv": "湖北卫视", "liaoningtv": "辽宁卫视", "heilongjiangtv": "黑龙江卫视",
    "sichuantv": "四川卫视", "chongqingtv": "重庆卫视", "tianjintv": "天津卫视",
    "hebeitv": "河北卫视", "jiangxitv": "江西卫视", "guangxitv": "广西卫视",
    "guizhoutv": "贵州卫视", "yunnansatellitetv": "云南卫视", "yunnantv": "云南卫视",
    "shanxitv": "山西卫视", "shaanxitv": "陕西卫视", "jilintv": "吉林卫视",
    "xinjiangtv": "新疆卫视", "hainantv": "海南卫视", "qinghaitv": "青海卫视",
    "gansutv": "甘肃卫视", "ningxiatv": "宁夏卫视", "xizangtv": "西藏卫视",
    "tibettv": "西藏卫视", "innermongoliatv": "内蒙古卫视",
}

IMPORTED_GROUP_ALIASES = (
    (("央视", "央视频道", "卫视台", "卫视频道", "卫视高清", "央视高清"), "大陆"),
    (("香港", "hong kong"), "香港"),
    (("澳门", "macao", "macau"), "澳门"),
    (("台湾", "taiwan"), "台湾"),
    (("新加坡", "singapore"), "新加坡"),
    (("马来西亚", "malaysia"), "马来西亚"),
    (("日本", "japan"), "日本"),
    (("韩国", "korea"), "韩国"),
    (("中文纪录",), "中文纪录"),
    (("中文电影",), "中文电影"),
    (("中文付费",), "中文付费"),
    (("纪录片", "documentary"), "纪录片"),
    (("少儿", "儿童", "kids"), "少儿"),
    (("体育", "sports"), "体育"),
    (("新闻", "news"), "新闻"),
    (("音乐", "music"), "音乐"),
    (("电影", "movies"), "电影"),
    (("国际", "global"), "国际"),
    (("娱乐", "entertainment"), "娱乐"),
    (("教育", "education"), "教育"),
    (("财经", "business"), "财经"),
)

FANMINGMING_LOGO_ALIASES = (
    (("cgtn纪录", "cgtn documentary", "cgtn doc"), "CGTN纪录"),
    (("cgtn俄语", "cgtn russian"), "CGTN俄语"),
    (("cgtn法语", "cgtn french"), "CGTN法语"),
    (("cgtn西语", "cgtn spanish", "cgtn espanol"), "CGTN西语"),
    (("cgtn阿语", "cgtn arabic"), "CGTN阿语"),
    (("cgtn",), "CGTN"),
    (("凤凰卫视中文", "凤凰中文"), "凤凰卫视中文台"),
    (("凤凰卫视资讯", "凤凰资讯"), "凤凰卫视资讯台"),
    (("凤凰卫视香港", "凤凰香港"), "凤凰卫视香港台"),
    (("tvb翡翠", "翡翠台"), "翡翠台"),
    (("tvb明珠", "明珠台"), "明珠台"),
    (("rthk31", "港台电视31"), "RTHK31"),
    (("rthk32", "港台电视32"), "RTHK32"),
    (("香港卫视",), "香港卫视"),
    (("澳门莲花", "莲花卫视"), "澳门莲花"),
    (("澳视澳门",), "澳视澳门"),
    (("澳亚卫视",), "澳亚卫视"),
)


@dataclass
class Channel:
    name: str
    extinf: str
    url: str
    group: str
    allow_geo: bool = False
    curated: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    static_score: float = 0.0
    probe: dict = field(default_factory=dict)
    display_override: str | None = None
    source: str = ""
    history: dict = field(default_factory=dict)


def load_health_history(path: Path = HISTORY_PATH) -> dict:
    """Load the bounded rolling route history written by earlier builds."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    routes = payload.get("routes", payload) if isinstance(payload, dict) else {}
    return routes if isinstance(routes, dict) else {}


def fetch_text(url: str, timeout: float = 25, headers: dict[str, str] | None = None, limit: int = 2_000_000) -> str:
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "application/vnd.apple.mpegurl,*/*"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(limit).decode("utf-8", "ignore")


def parse_headers(extinf: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    ua = re.search(r'http-user-agent="([^"]+)"', extinf, re.I)
    ref = re.search(r'http-referrer="([^"]+)"', extinf, re.I)
    if ua:
        headers["User-Agent"] = ua.group(1)
    if ref:
        headers["Referer"] = ref.group(1)
    return headers


def normalized_imported_group(value: str, fallback: str) -> str:
    low = re.sub(r"\s+", " ", value).strip().lower()
    if value.strip() in {*MAINLAND_REGION_GROUPS, "其他地方"}:
        return value.strip()
    for aliases, canonical in IMPORTED_GROUP_ALIASES:
        if any(alias in low for alias in aliases):
            return canonical
    return fallback


def parse_m3u(text: str, group: str, allow_geo: bool, source: str = "") -> list[Channel]:
    """Parse ordinary M3U plus common Chinese name,url TXT playlists."""
    channels: list[Channel] = []
    extinf: str | None = None

    def append_channel(name: str, item_extinf: str, url: str) -> None:
        clean_url = url.split("$", 1)[0].strip()
        if not clean_url.startswith(("http://", "https://")):
            return
        low = f"{name} {item_extinf}".lower()
        assigned_group = group
        # When carrying the last published playlist into the next health scan,
        # preserve its real category instead of flattening all 600 channels.
        declared_group = re.search(r'group-title="([^"]+)"', item_extinf, re.I)
        if declared_group:
            fallback = "中文综合" if group == "现有订阅" else group
            assigned_group = normalized_imported_group(declared_group.group(1), fallback)
        elif group == "现有订阅":
            assigned_group = "中文综合"
        identity_extinf = re.sub(r'group-title="[^"]*"', "", item_extinf, flags=re.I)
        chinese_identity = bool(re.search(r"[\u4e00-\u9fff]", name)) or bool(
            re.search(r"(?:cctv|cgtn|tvb|phoenix|rthk|hoy|china|chinese|taiwan|hong\s*kong|macau)", f"{name} {identity_extinf}", re.I)
        )
        if group == "中文付费" and not any(token in low for token in PUBLIC_PAY_HINTS):
            assigned_group = "大陆" if chinese_identity else "娱乐"
        elif group == "中文综合" and not chinese_identity:
            assigned_group = "娱乐"
        if any(word in low for word in BLOCK_WORDS):
            return
        if "geo-blocked" in low and not allow_geo:
            return
        if ".mpd" in clean_url.lower():
            return
        channels.append(
            Channel(
                name,
                item_extinf,
                clean_url,
                assigned_group,
                allow_geo,
                False,
                parse_headers(item_extinf),
                source=source,
            )
        )

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            extinf = line
            continue
        if line.startswith("#"):
            continue
        if extinf and line.startswith(("http://", "https://")):
            name = extinf.split(",", 1)[1].strip() if "," in extinf else "Unknown"
            append_channel(name, extinf, line)
            extinf = None
            continue

        # Chinese TXT lists commonly use "频道名,url1#url2$metadata".
        if "," in line:
            name, url_blob = (part.strip() for part in line.split(",", 1))
            if not name or "#genre#" in url_blob.lower() or re.match(r"^\d{8}(?:\s|$)", name):
                continue
            item_extinf = f'#EXTINF:-1 group-title="{group}",{name}'
            for route in url_blob.split("#"):
                append_channel(name, item_extinf, route)
    return channels


def normalized_name(name: str) -> str:
    value = re.sub(r"\([^)]*(?:\d{3,4}[pi]|geo|not 24|hd|sd)[^)]*\)", "", name, flags=re.I)
    value = re.sub(r"\[[^]]*\]", "", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def canonical_mainland_satellite_name(channel: Channel) -> str | None:
    """Map only a visible provincial satellite label to its canonical name.

    Imported group-title metadata is intentionally ignored: many source lists
    put local, sports and VOD entries in a generic satellite section.
    """
    visible = normalized_name(channel.name)
    for alias, canonical in MAINLAND_SATELLITE_CHINESE_ALIASES.items():
        if alias in visible:
            return canonical
    for canonical in MAINLAND_SATELLITE_NAMES:
        if canonical in visible:
            return canonical
    compact = re.sub(r"[^a-z0-9]+", "", visible)
    compact = re.sub(r"(?:uhd|fhd|hd|sd|4k|8k|2160p?|1080p?|720p?)$", "", compact)
    return MAINLAND_SATELLITE_ENGLISH_ALIASES.get(compact)


def required_core_id(channel: Channel) -> str | None:
    """Return the canonical id for the five user-required CCTV stations."""
    value = f"{channel.name} {channel.extinf}".lower()
    if re.search(r"cctv[\s_-]*0?5\s*(?:\+|plus|p)", value, re.I):
        return "cctv5plus"
    match = re.search(r"cctv[\s_-]*0?(5|9|12|16)(?!\d)", value, re.I)
    return f"cctv{match.group(1)}" if match else None


def is_cctv4k_label(channel: Channel) -> bool:
    label = normalized_name(channel.name)
    return bool(re.search(r"cctv[\s_-]*4[\s_-]*k", label, re.I))


def is_cctv8k_label(channel: Channel) -> bool:
    """Keep the CCTV-8K ultra-HD service separate from CCTV-8 Drama."""
    label = normalized_name(channel.name)
    return bool(re.search(r"cctv[\s_-]*8[\s_-]*k", label, re.I))


def numbered_cctv_id(channel: Channel) -> str | None:
    """Return CCTV-1..17, excluding distinct CCTV-4K/CCTV-8K services."""
    if is_cctv4k_label(channel) or is_cctv8k_label(channel):
        return None
    match = re.search(r"cctv[\s_-]*0?(\d{1,2})(?!\d)", normalized_name(channel.name), re.I)
    if match and 1 <= int(match.group(1)) <= 17:
        return f"cctv{int(match.group(1))}"
    return None


def cctv_url_conflicts_with_label(channel: Channel) -> bool:
    """Reject obvious CCTV label/path conflicts before expensive probes."""
    expected = numbered_cctv_id(channel)
    if expected is None:
        return False
    url_low = urllib.parse.unquote(channel.url).lower()
    if "cgtn-america.m3u8" in url_low:
        return True
    if expected == "cctv8" and re.search(
        r"(?:[?&]id=cctv8k\b|channel_cctv8k|/cctv8k(?:[/?.]|$))",
        url_low,
    ):
        return True
    url_numbers = {
        f"cctv{int(number)}"
        for number in re.findall(r"cctv[\s_/-]*0?(\d{1,2})(?!\d)", url_low, re.I)
        if 1 <= int(number) <= 17
    }
    return bool(url_numbers and expected not in url_numbers)


def channel_key(channel: Channel) -> str:
    """Canonical key used only after alternate URLs have been speed-tested."""
    visible_name = normalized_name(channel.name)
    # CCTV-4 (Chinese International) and CCTV-4K are separate channels, while
    # spelling variants such as CCTV4K / CCTV-4K HD are one channel.
    if is_cctv4k_label(channel):
        return "cctv4k"
    if is_cctv8k_label(channel):
        return "cctv8k"
    satellite_name = canonical_mainland_satellite_name(channel)
    if satellite_name:
        return satellite_name
    # Prefer the visible CCTV name. Some imported lists use numeric tvg-id="5"
    # or tvg-id="6", which must not split CCTV-5/5+ into unrelated keys.
    required = required_core_id(channel)
    if required:
        return required
    cctv_id = numbered_cctv_id(channel)
    if cctv_id:
        return cctv_id
    # Lists often assign inconsistent tvg-id values to the same station. Use
    # the cleaned visible label for every language so alternate routes cannot
    # inflate the published channel count.
    visible_clean = re.sub(r"\s*(?:高清|超清|蓝光|hd|fhd|uhd|4k|8k|1080p?|720p?)$", "", visible_name, flags=re.I)
    visible_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", visible_clean)
    if visible_key and visible_key not in {"unknown", "channel", "tv"}:
        return visible_key
    if "卫视" in visible_name:
        # Several lists assign different numeric tvg-id values to the same
        # satellite station. Use its display name so those routes race instead
        # of becoming duplicate APTV tiles.
        visible_name = re.sub(r"^(?:brtv|btv)\s*", "", visible_name, flags=re.I)
        visible_name = re.sub(r"\s*(?:高清|超清|hd)$", "", visible_name, flags=re.I)
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", visible_name)
    tvg_id = re.search(r'tvg-id="([^"]+)"', channel.extinf, re.I)
    if tvg_id:
        value = re.sub(r"@.*$", "", tvg_id.group(1).lower())
        value = re.sub(r"\.(?:cn|hk|mo|tw|sg|my|jp|kr)$", "", value)
        value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)
        if value:
            return value
    value = normalized_name(channel.name)
    value = re.sub(r"cctv[\s_-]*0?(\d+)", r"cctv\1", value, flags=re.I)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)


def route_history_key(channel: Channel) -> str:
    """Keep history across rotating auth query strings for the same route."""
    parsed = urllib.parse.urlsplit(channel.url)
    host = (parsed.hostname or "").lower()
    try:
        port_number = parsed.port
    except ValueError:
        port_number = None
    port = f":{port_number}" if port_number else ""
    path = re.sub(r"/+", "/", urllib.parse.unquote(parsed.path or "/"))
    return f"{channel_key(channel)}|{parsed.scheme.lower()}://{host}{port}{path}"


def attach_health_history(channels: Iterable[Channel], history: dict) -> None:
    for channel in channels:
        record = history.get(route_history_key(channel), {})
        channel.history = record if isinstance(record, dict) else {}


def historical_score(channel: Channel) -> float:
    """Reward routes that keep passing across builds, not one lucky segment."""
    recent = [1 if value else 0 for value in channel.history.get("recent", [])][-HISTORY_WINDOW:]
    if not recent:
        return 0.0
    success_ratio = sum(recent) / len(recent)
    score = (success_ratio - 0.5) * 150
    streak = 0
    for value in reversed(recent):
        if not value:
            break
        streak += 1
    score += min(streak, 6) * 9
    if len(recent) >= 4 and success_ratio < 0.6:
        score -= 80
    if not recent[-1]:
        score -= 45
    return score


def update_health_history(channels: Iterable[Channel], previous: dict) -> dict:
    """Return a compact history document for routes actually tested today."""
    routes = {
        key: dict(value)
        for key, value in previous.items()
        if isinstance(value, dict)
    }
    for channel in channels:
        key = route_history_key(channel)
        record = routes.get(key, {})
        recent = [1 if value else 0 for value in record.get("recent", [])][-HISTORY_WINDOW + 1:]
        probe = channel.probe
        success = bool(
            probe.get("ok")
            and int(probe.get("checks_ok") or 1) >= 2
            and not probe.get("recheck_failed")
            and not probe.get("easy_check_failed")
            and not probe.get("duplicate_core_content")
        )
        recent.append(1 if success else 0)
        routes[key] = {
            "recent": recent[-HISTORY_WINDOW:],
            "last_seen_utc": GENERATED_AT,
            "last_ok": success,
            "last_checks_ok": int(probe.get("checks_ok") or 0),
            "last_mbps": round(float(probe.get("segment_mbps") or 0), 2),
            "last_stream_mbps": round(float(probe.get("stream_mbps") or 0), 2),
            "last_manifest_s": round(float(probe.get("manifest_s") or 0), 3),
            "ipv4_dns": bool(probe.get("ipv4_dns")),
            "ipv6_dns": bool(probe.get("ipv6_dns")),
        }

    # A removed public route should not grow this generated file forever.
    if len(routes) > 6000:
        routes = dict(
            sorted(
                routes.items(),
                key=lambda item: item[1].get("last_seen_utc", ""),
                reverse=True,
            )[:6000]
        )
    return {
        "version": 1,
        "generated_utc": GENERATED_AT,
        "window": HISTORY_WINDOW,
        "routes": routes,
    }


def is_core_channel(channel: Channel) -> bool:
    visible = normalized_name(channel.name)
    return bool(re.search(r"\bcctv[\s_-]*\d+", visible, re.I)) or canonical_mainland_satellite_name(channel) is not None


def is_viewer_wanted_channel(channel: Channel) -> bool:
    """Enforce the requested Chinese/Taiwan/Japan, no-English profile."""
    if channel.group in BLOCKED_OUTPUT_GROUPS:
        return False
    # A catch-all group label such as group-title="中文综合" is not language
    # evidence for the station itself.  Excluding it here keeps English-named
    # overflow from occupying main-list slots only to be removed by the final
    # publication guard.
    identity_extinf = re.sub(r'group-title="[^"]*"', "", channel.extinf, flags=re.I)
    identity = f"{channel.name} {identity_extinf}"
    if re.search(r"(?<![a-z])cgtn(?![a-z])", identity, re.I):
        return False
    if ENGLISH_SERVICE_RE.search(identity):
        return False
    if is_core_channel(channel):
        return True
    if channel.group == "新加坡":
        return bool(
            re.search(
                r"(?:channel\s*(?:8|u)\b|8\s*频道|8頻道|u\s*频道|u頻道|华语|華語|中文)",
                identity,
                re.I,
            )
        )
    if channel.group in {"台湾", "日本"}:
        return True
    if channel.group == "香港":
        return bool(CJK_RE.search(identity) or REGIONAL_BRAND_RE.search(identity))
    if channel.group == "澳门":
        return bool(CJK_RE.search(identity) or re.search(r"\btdm\b", identity, re.I))
    return bool(CJK_RE.search(identity) or REGIONAL_BRAND_RE.search(identity))


def is_placeholder_relay(channel: Channel) -> bool:
    host = (urllib.parse.urlsplit(channel.url).hostname or "").lower()
    return host in PLACEHOLDER_RELAY_HOSTS or host == "freetv.fun" or host.endswith(".freetv.fun")


def is_station_like(channel: Channel) -> bool:
    """Reject radio, movies, episodes and other entries that are not linear TV."""
    name_low = channel.name.lower()
    low = f"{channel.name} {channel.extinf}".lower()
    if channel.url.rstrip("/") in {url.rstrip("/") for url in MISLABELLED_STREAM_URLS}:
        return False
    if channel.url.startswith(USER_REJECTED_ROUTE_PREFIXES):
        return False
    if cctv_url_conflicts_with_label(channel):
        return False
    # Some public lists attach AAC-only feeds to CCTV video labels. They pass
    # HLS/segment checks but produce a black screen with sound in APTV.
    url_path = urllib.parse.urlsplit(channel.url).path.lower()
    if "/audio/" in url_path:
        return False
    # Public lists often mislabel CCTV-5+ paths (cctv5p/cctv5plus) as CCTV-5.
    # Keep sports and sports-events routes separate even when both are live.
    if re.search(r"cctv5(?:p|plus)", url_path, re.I) and required_core_id(channel) != "cctv5plus":
        return False
    url_low = channel.url.lower()
    if re.search(r"(?:[?&]id=cctv4k\b|channel_cctv4k|/cctv4k(?:[/?.]|$))", url_low) and not is_cctv4k_label(channel):
        return False
    if any(token in name_low for token in NON_TELEVISION_HINTS) and not any(
        token in name_low for token in ("广播电视", "电视", "频道")
    ):
        return False
    if PROGRAM_TITLE_RE.search(channel.name) and not CLEAR_STATION_RE.search(low):
        return False
    if re.search(r'tvg-(?:id|name|logo)="[^"]+"', channel.extinf, re.I):
        return True
    return any(token in low for token in STATION_NAME_HINTS)


def is_public_pay_channel(channel: Channel) -> bool:
    low = f"{channel.name} {channel.extinf}".lower()
    return any(token in low for token in PUBLIC_PAY_HINTS)


def is_chinese_oriented(channel: Channel) -> bool:
    identity_extinf = re.sub(r'group-title="[^"]*"', "", channel.extinf, flags=re.I)
    identity = f"{channel.name} {identity_extinf}".lower()
    return (
        channel.group in CHINESE_GROUPS
        or bool(re.search(r"[\u4e00-\u9fff]", channel.name))
        or bool(re.search(r"(?:cctv|cgtn|tvb|phoenix|hoy|rthk|china|chinese|taiwan|hong\s*kong|macau)", identity, re.I))
    )


def labelled_height(channel: Channel) -> int:
    low = f"{channel.name} {channel.extinf}".lower()
    if "8k" in low or "4320" in low:
        return 4320
    if "4k" in low or "2160" in low:
        return 2160
    for height in (1440, 1080, 720, 576, 540, 480, 360, 240, 180):
        if str(height) in low:
            return height
    return 0



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


def channel_static_score(channel: Channel) -> float:
    low = f"{channel.name} {channel.extinf}".lower()
    parsed = urllib.parse.urlsplit(channel.url)
    host = (parsed.hostname or "").lower()
    score = 0.0
    if channel.url.startswith("https://"):
        score += 26
    if any(token in host for token in PREFERRED_HOSTS):
        score += 42
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        score -= 24
    if parsed.port and parsed.port not in (80, 443):
        score -= 12
    if any(token in host for token in UNSTABLE_HOST_HINTS):
        score -= 28
    if channel_key(channel) == "cctv5":
        # Smoothness on the viewer's route matters more than the resolution
        # label or one short segment burst measured in GitHub Actions.
        if any(token in host for token in CCTV5_EDGE_HOSTS):
            score += 190
        if channel.url in CCTV5_PREFERRED_1080_URLS:
            score += 260
        if any(token in host for token in CCTV5_OPERATOR_HINTS):
            score -= 90
    if is_placeholder_relay(channel):
        score -= 500
    if "not 24/7" in low:
        score -= 18
    if any(token in low for token in LOW_VALUE):
        score -= 8
    height = labelled_height(channel)
    if height == 1080:
        score += 55
    elif height == 720:
        score += 15
    elif height > 1080:
        score += 25  # Keep UHD, while making 1080p the normal main-list target.
    elif 0 < height < 720:
        score -= 16
    if channel.group in CHINESE_GROUPS:
        score += 28
    if re.search(r"[\u4e00-\u9fff]", channel.name):
        score += 22
    if channel.headers:
        score -= 8
    if channel.curated:
        score += 6
    if channel.url.startswith(USER_PREFERRED_ROUTE_PREFIXES):
        score += 220
    # A fast county station must not crowd CCTV and major satellite channels
    # out of a 200-tile living-room playlist.
    if re.search(r"\bcctv[\s_-]*\d+", low, re.I) or "央视" in low:
        score += 110
    elif canonical_mainland_satellite_name(channel):
        score += 55
    elif channel.group == "中文付费":
        score += 35
    if is_public_pay_channel(channel):
        score += 28
    return score


def variant_rows(text: str, base_url: str) -> list[tuple[int, int, int, str]]:
    rows: list[tuple[int, int, int, str]] = []
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
        width = int(resolution.group(1)) if resolution else 0
        height = int(resolution.group(2)) if resolution else 0
        rate = int(bandwidth.group(1)) if bandwidth else 0
        for candidate in lines[index + 1:]:
            if not candidate or candidate.startswith("#"):
                continue
            rows.append((width, height, rate, urllib.parse.urljoin(base_url, candidate)))
            break
    return rows



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


def first_media_segment(text: str, base_url: str) -> tuple[str | None, float]:
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF:"):
            match = re.match(r"#EXTINF:([0-9.]+)", line, re.I)
            duration = float(match.group(1)) if match else 0.0
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")) or not urllib.parse.urlsplit(line).scheme:
            return urllib.parse.urljoin(base_url, line), duration
    return None, 0.0


def first_media_uri(text: str, base_url: str) -> str | None:
    return first_media_segment(text, base_url)[0]


def timed_read_with_size(
    url: str,
    headers: dict[str, str],
    limit: int,
    timeout: float,
) -> tuple[bytes, float, str, int]:
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    request_headers.update(headers)
    request_headers["Range"] = f"bytes=0-{limit - 1}"
    req = urllib.request.Request(url, headers=request_headers)
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        final_url = response.geturl()
        data = response.read(limit)
        resource_size = 0
        content_range = response.headers.get("Content-Range", "")
        range_match = re.search(r"/([0-9]+)$", content_range)
        if range_match:
            resource_size = int(range_match.group(1))
        elif response.headers.get("Content-Length"):
            try:
                resource_size = int(response.headers["Content-Length"])
            except (TypeError, ValueError):
                resource_size = 0
        if not resource_size and len(data) < limit:
            resource_size = len(data)
    return data, max(time.monotonic() - started, 0.001), final_url, resource_size


def timed_read(url: str, headers: dict[str, str], limit: int, timeout: float) -> tuple[bytes, float, str]:
    data, elapsed, final_url, _ = timed_read_with_size(url, headers, limit, timeout)
    return data, elapsed, final_url


@functools.lru_cache(maxsize=2048)
def host_ip_families(host: str) -> tuple[bool, bool]:
    """Return DNS availability as (IPv4, IPv6), including literal hosts."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.version == 4, literal.version == 6
    try:
        families = {
            info[0]
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except (OSError, socket.gaierror):
        return False, False
    return socket.AF_INET in families, socket.AF_INET6 in families


def url_ip_families(url: str) -> tuple[bool, bool]:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host_ip_families(host) if host else (False, False)



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


def probe_channel(channel: Channel) -> dict:
    errors: list[str] = []
    attempts = [False]
    if channel.headers:
        attempts.append(True)
    for use_headers in attempts:
        try:
            return probe_once(channel, use_headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 451) and channel.allow_geo:
                return {"ok": False, "geo_restricted": True, "error": f"http_{exc.code}"}
            errors.append(f"http_{exc.code}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as exc:
            errors.append(type(exc).__name__ + ":" + str(exc)[:80])
    return {"ok": False, "error": errors[-1] if errors else "probe_failed"}


def merge_probe_results(first: dict, later: dict, checks_ok: int) -> dict:
    """Keep the slowest observed speed and worst startup across fresh checks."""
    combined = dict(first)
    combined["checks_ok"] = checks_ok
    combined["segment_mbps"] = min(
        float(first.get("segment_mbps") or 0),
        float(later.get("segment_mbps") or 0),
    )
    combined["manifest_s"] = max(
        float(first.get("manifest_s") or 0),
        float(later.get("manifest_s") or 0),
    )
    stream_rates = [
        float(value)
        for value in (first.get("stream_mbps"), later.get("stream_mbps"))
        if value is not None and float(value) > 0
    ]
    combined["stream_mbps"] = min(stream_rates, default=0.0)
    first_height = int(first.get("height") or 0)
    later_height = int(later.get("height") or 0)
    combined["height"] = min(
        (height for height in (first_height, later_height) if height),
        default=0,
    )
    combined["ipv4_dns"] = bool(first.get("ipv4_dns") or later.get("ipv4_dns"))
    combined["ipv6_dns"] = bool(first.get("ipv6_dns") or later.get("ipv6_dns"))
    combined["dual_stack_dns"] = bool(
        combined["ipv4_dns"] and combined["ipv6_dns"]
    )
    fingerprints = {
        str(value)
        for probe in (first, later)
        for value in (
            list(probe.get("segment_fingerprints") or [])
            + ([probe.get("segment_sha256")] if probe.get("segment_sha256") else [])
        )
        if value
    }
    combined["segment_fingerprints"] = sorted(fingerprints)
    return combined


def mark_duplicate_core_content(
    channels: Iterable[Channel], min_distinct_keys: int = 4
) -> list[dict]:
    """Reject one video feed relabelled as many CCTV/satellite channels.

    A legitimate simulcast can occasionally share a commercial, so a pair is
    not enough. Four distinct canonical core stations returning an identical
    media segment is treated as a poisoned relay and blocks every matching
    candidate before selection.
    """
    by_fingerprint: dict[str, list[Channel]] = defaultdict(list)
    for channel in channels:
        channel.probe.pop("duplicate_core_content", None)
        channel.probe.pop("duplicate_content_keys", None)
        if not is_core_channel(channel) or not channel.probe.get("ok"):
            continue
        fingerprints = set(channel.probe.get("segment_fingerprints") or [])
        if channel.probe.get("segment_sha256"):
            fingerprints.add(str(channel.probe["segment_sha256"]))
        for fingerprint in fingerprints:
            by_fingerprint[fingerprint].append(channel)

    collisions: list[dict] = []
    for fingerprint, matches in by_fingerprint.items():
        keys = sorted({channel_key(channel) for channel in matches if channel_key(channel)})
        if len(keys) < min_distinct_keys:
            continue
        for channel in matches:
            channel.probe["duplicate_core_content"] = True
            channel.probe["duplicate_content_keys"] = keys
        collisions.append(
            {
                "fingerprint": fingerprint[:16],
                "distinct_keys": len(keys),
                "keys": keys,
                "urls": sorted({channel.url for channel in matches}),
            }
        )
    return sorted(collisions, key=lambda item: (-item["distinct_keys"], item["fingerprint"]))



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


def route_selection_score(channel: Channel) -> float:
    return core_route_score(channel) if is_core_channel(channel) else measured_score(channel)


def measured_score(channel: Channel) -> float:
    score = channel.static_score + historical_score(channel)
    probe = channel.probe
    if probe.get("geo_restricted"):
        return score - 25
    if not probe.get("ok") or probe.get("duplicate_core_content"):
        return -9999
    speed = min(float(probe.get("segment_mbps") or 0), 40)
    latency = float(probe.get("manifest_s") or 10)
    height = int(probe.get("height") or labelled_height(channel))
    stream_mbps = float(probe.get("stream_mbps") or 0)
    score += speed * (0.8 if is_core_channel(channel) else 2.2) - latency * 7
    if int(probe.get("checks_ok") or 1) >= 2:
        score += 35
    elif probe.get("recheck_failed"):
        score -= 65
    if height == 1080:
        score += 70
    elif height == 720:
        score += 20
    elif height > 1080:
        score += 25
    elif height and height < 720:
        score -= 30
    # Download throughput answers "will it buffer?"; HLS segment bitrate
    # answers "how clear is the programme?". Give CCTV/satellite selection a
    # real quality signal even when an operator serves a direct media playlist
    # without RESOLUTION/BANDWIDTH tags (the CCTV-10 case reported at home).
    if stream_mbps:
        quality = min(stream_mbps, 12.0)
        headroom = speed / stream_mbps
        if is_core_channel(channel):
            if stream_mbps < 1.2:
                score -= 130
            elif headroom >= 1.50:
                score += quality * 25 + min(headroom, 3.0) * 7
            elif headroom >= 1.25:
                score += quality * 18
            elif headroom >= 1.05:
                score += quality * 8 - 70
            else:
                score -= 260
        else:
            score += quality * 5
            if headroom < 1.05:
                score -= 70
    # The viewer has native IPv6. Prefer dual-stack CDN/domain routes modestly,
    # while retaining IPv4 fallback; DNS capability alone never overrides a
    # failed HLS/video-segment probe.
    if probe.get("ipv6_dns"):
        score += 18
    if probe.get("dual_stack_dns"):
        score += 8
    return score


def core_candidate_diagnostics(channels: Iterable[Channel], targets: Iterable[str]) -> dict[str, list[dict]]:
    """Compact failure evidence for a core safety stop in Actions logs."""
    output: dict[str, list[dict]] = {}
    for target in targets:
        if target in CCTV_AUDIT_IDS:
            tested = [channel for channel in channels if channel_key(channel) == target]
        else:
            tested = [
                channel for channel in channels
                if canonical_mainland_satellite_name(channel) == target
            ]
        tested.sort(key=route_selection_score, reverse=True)
        output[target] = [
            {
                "name": channel.name,
                "url": channel.url,
                "ok": bool(channel.probe.get("ok")),
                "checks_ok": int(channel.probe.get("checks_ok") or 0),
                "height": int(channel.probe.get("height") or labelled_height(channel)),
                "download_mbps": round(float(channel.probe.get("segment_mbps") or 0), 2),
                "stream_mbps": round(float(channel.probe.get("stream_mbps") or 0), 2),
                "duplicate_core_content": bool(
                    channel.probe.get("duplicate_core_content")
                ),
                "error": str(
                    channel.probe.get("easy_check_error")
                    or channel.probe.get("recheck_error")
                    or channel.probe.get("error")
                    or ""
                )[:100],
            }
            for channel in tested[:12]
        ]
    return output


def deduplicate(channels: Iterable[Channel]) -> list[Channel]:
    best_by_url: dict[str, Channel] = {}
    for channel in channels:
        if not is_station_like(channel) or not is_viewer_wanted_channel(channel):
            continue
        channel.static_score = channel_static_score(channel)
        existing = best_by_url.get(channel.url)
        if existing is None or channel.static_score > existing.static_score:
            best_by_url[channel.url] = channel

    variants: dict[str, list[Channel]] = defaultdict(list)
    for channel in best_by_url.values():
        key = channel_key(channel)
        if key:
            variants[key].append(channel)

    result: list[Channel] = []
    for items in variants.values():
        items.sort(key=lambda item: (item.curated, item.static_score), reverse=True)
        if required_core_id(items[0]):
            limit = 24
        elif is_core_channel(items[0]):
            limit = CORE_VARIANTS_PER_CHANNEL
        else:
            limit = MAX_VARIANTS_PER_CHANNEL
        result.extend(items[:limit])
    return result


def select_probe_pool(channels: list[Channel]) -> list[Channel]:
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in channels:
        grouped[channel.group].append(channel)
    pool: list[Channel] = []
    for group, items in grouped.items():
        quota = PROBE_GROUP_TARGETS.get(group, GROUP_TARGETS.get(group, 5))
        by_channel: dict[str, list[Channel]] = defaultdict(list)
        for item in items:
            by_channel[channel_key(item)].append(item)
        for variants in by_channel.values():
            variants.sort(key=lambda item: item.static_score, reverse=True)

        # Probe up to 24 independent routes for the five critical CCTV stations
        # and up to 16 for every other CCTV/provincial satellite station before
        # the ordinary breadth-first pool. Domestic CDN routes often reject a
        # US GitHub runner while another route remains healthy.
        group_pool: list[Channel] = []
        for target in REQUIRED_CORE_IDS:
            group_pool.extend(by_channel.get(target, [])[:24])
        for variants in by_channel.values():
            if (
                variants
                and not required_core_id(variants[0])
                and variants[0].group == "大陆"
                and is_core_channel(variants[0])
            ):
                for channel in variants[:CORE_VARIANTS_PER_CHANNEL]:
                    if channel not in group_pool:
                        group_pool.append(channel)

        # Breadth first: test one URL for many different channels before using
        # remaining slots on alternate URLs for CCTV/卫视 and other duplicates.
        unique_limit = max(45, quota * 4)
        ranked_keys = sorted(
            by_channel,
            key=lambda key: by_channel[key][0].static_score,
            reverse=True,
        )[:unique_limit]
        for key in ranked_keys:
            first = by_channel[key][0]
            if first not in group_pool:
                group_pool.append(first)
        group_limit = max(80, quota * 6)
        variant_index = 1
        while len(group_pool) < group_limit:
            added = False
            for key in ranked_keys:
                variants = by_channel[key]
                if variant_index < len(variants):
                    group_pool.append(variants[variant_index])
                    added = True
                    if len(group_pool) >= group_limit:
                        break
            if not added:
                break
            variant_index += 1
        pool.extend(group_pool)
    # Curated entries must always be checked, even when their static score is low.
    for channel in channels:
        if channel.curated and channel not in pool:
            pool.append(channel)
    return pool



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


def add_cctv5_backups(stable: list[Channel], channels: list[Channel], count: int = 2) -> list[Channel]:
    """Publish two independently hosted 1080p CCTV-5 escape routes.

    APTV does not fail over between alternate URLs hidden behind one tile. Two
    clearly named backup tiles are therefore more useful than letting a remote
    build runner guess which single route matches the viewer's ISP/VPN path.
    """
    primary = next((channel for channel in stable if channel_key(channel) == "cctv5"), None)
    if primary is None:
        return stable
    used_urls = {primary.url}
    used_hosts = {(urllib.parse.urlsplit(primary.url).hostname or "").lower()}
    candidates = [
        channel for channel in channels
        if channel_key(channel) == "cctv5"
        and channel.url not in used_urls
        and channel.probe.get("ok")
        and not channel.probe.get("header_required")
        and int(channel.probe.get("height") or labelled_height(channel)) >= 1080
        and "/audio/" not in urllib.parse.urlsplit(channel.url).path.lower()
        and not is_placeholder_relay(channel)
    ]
    candidates.sort(
        key=lambda channel: (
            int(channel.probe.get("checks_ok") or 1) >= 2,
            not bool(channel.probe.get("recheck_failed")),
            core_route_score(channel),
            -float(channel.probe.get("manifest_s") or 99),
        ),
        reverse=True,
    )
    backups: list[Channel] = []
    for candidate in candidates:
        host = (urllib.parse.urlsplit(candidate.url).hostname or "").lower()
        if host in used_hosts:
            continue
        index = len(backups) + 1
        display_name = f"CCTV-5 备用{index} 1080p"
        extinf = candidate.extinf
        if "," in extinf:
            extinf = extinf.rsplit(",", 1)[0] + "," + display_name
        backups.append(
            Channel(
                name=display_name,
                extinf=extinf,
                url=candidate.url,
                group="大陆",
                allow_geo=candidate.allow_geo,
                curated=candidate.curated,
                headers=dict(candidate.headers),
                static_score=candidate.static_score,
                probe=dict(candidate.probe),
                display_override=display_name,
                source=candidate.source,
            )
        )
        used_hosts.add(host)
        if len(backups) >= count:
            break
    if not backups:
        return stable

    # Preserve the requested main-list size by replacing the lowest-scoring
    # non-core/non-pay entries, favouring removal of non-Chinese overflow.
    remove_count = max(0, len(stable) + len(backups) - TARGET_STABLE)
    removable = sorted(
        (
            channel for channel in stable
            if not is_core_channel(channel) and not is_public_pay_channel(channel)
        ),
        key=lambda channel: (is_chinese_oriented(channel), measured_score(channel)),
    )
    remove_urls = {channel.url for channel in removable[:remove_count]}
    output = [channel for channel in stable if channel.url not in remove_urls]
    output.extend(backups)
    return output[:TARGET_STABLE]


def publication_fallback_rank(channel: Channel) -> tuple:
    """Rank broader-list fallbacks without pretending all were stable."""
    probe = channel.probe
    ok = bool(probe.get("ok")) and not bool(
        probe.get("header_required") or probe.get("duplicate_core_content")
    )
    quality_score = (
        route_selection_score(channel) if ok else channel.static_score
    )
    return (
        ok and int(probe.get("checks_ok") or 0) >= 2,
        ok,
        str(channel.source).startswith("carried:"),
        channel.curated,
        int(probe.get("height") or labelled_height(channel)) >= 720,
        quality_score,
    )



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


def display_group(channel: Channel) -> str:
    """Return the final APTV group, splitting catch-all Chinese stations."""
    identity_extinf = re.sub(r'group-title="[^"]*"', "", channel.extinf, flags=re.I)
    identity = f"{channel.name} {identity_extinf}"
    # CCTV can arrive through language/category feeds instead of the old
    # Mainland feed, so classify it independently of the imported group.
    if re.search(r"(?:cctv|cgtn|央视)", identity, re.I):
        return "卫视台"
    if channel.group in {"大陆", "中文综合"} and canonical_mainland_satellite_name(channel):
        return "卫视台"
    return regionalized_group(identity, channel.group)


def canonical_display_name(channel: Channel) -> str:
    if channel.display_override:
        return channel.display_override
    key = channel_key(channel)
    if key == "cctv4k":
        return "CCTV-4K"
    if key == "cctv8k":
        return "CCTV-8K"
    if key == "cctv5plus":
        return "CCTV-5+"
    numbered = re.fullmatch(r"cctv(\d{1,2})", key)
    if numbered:
        return f"CCTV-{int(numbered.group(1))}"
    satellite_name = canonical_mainland_satellite_name(channel)
    if satellite_name:
        return satellite_name
    return channel.name


def fanmingming_logo_name(channel: Channel) -> str | None:
    """Return a verified fanmingming logo/EPG key for common Chinese stations."""
    key = channel_key(channel)
    if key == "cctv4k":
        return "CCTV4K"
    if key == "cctv8k":
        return "CCTV8K"
    if key == "cctv5plus":
        return "CCTV5+"
    numbered = re.fullmatch(r"cctv(\d{1,2})", key)
    if numbered:
        return f"CCTV{int(numbered.group(1))}"
    satellite_name = canonical_mainland_satellite_name(channel)
    if satellite_name:
        return satellite_name
    identity = normalized_name(f"{canonical_display_name(channel)} {channel.name}")
    for aliases, logo_name in FANMINGMING_LOGO_ALIASES:
        if any(alias in identity for alias in aliases):
            return logo_name
    return None


def set_extinf_attribute(extinf: str, name: str, value: str) -> str:
    escaped = value.replace('"', "")
    pattern = rf'\s+{re.escape(name)}="[^"]*"'
    replacement = f' {name}="{escaped}"'
    if re.search(pattern, extinf, re.I):
        return re.sub(pattern, replacement, extinf, count=1, flags=re.I)
    return extinf.replace("#EXTINF:-1", f"#EXTINF:-1{replacement}", 1)


def cleaned_extinf(channel: Channel) -> str:
    extinf = channel.extinf
    group = display_group(channel)
    if "group-title=" in extinf:
        extinf = re.sub(r'group-title="[^"]*"', f'group-title="{group}"', extinf)
    else:
        extinf = extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{group}"', 1)
    display_name = canonical_display_name(channel)
    if "," in extinf and display_name != channel.name:
        extinf = extinf.rsplit(",", 1)[0] + "," + display_name
    metadata_name = fanmingming_logo_name(channel)
    if metadata_name:
        logo_name = urllib.parse.quote(metadata_name, safe="+")
        extinf = set_extinf_attribute(extinf, "tvg-id", metadata_name)
        extinf = set_extinf_attribute(extinf, "tvg-name", metadata_name)
        extinf = set_extinf_attribute(extinf, "tvg-logo", f"{LOGO_BASE_URL}/{logo_name}.png")
    return extinf


def satellite_sort_key(channel: Channel) -> tuple:
    if display_group(channel) != "卫视台":
        return (9, 999, 9)
    key = channel_key(channel)
    backup = re.search(r"CCTV-5\s*备用(\d+)", channel.display_override or "", re.I)
    if backup:
        return (0, 5, int(backup.group(1)))
    if key == "cctv4k":
        return (0, 18, 0)
    if key == "cctv5plus":
        return (0, 5, 3)
    numbered = re.fullmatch(r"cctv(\d{1,2})", key)
    if numbered:
        return (0, int(numbered.group(1)), 0)
    identity = f"{channel.name} {channel.extinf}".lower()
    if "cgtn" in identity:
        return (1, 0, 0)
    return (2, 0, 0)


def sort_channels(channels: list[Channel]) -> list[Channel]:
    return sorted(
        channels,
        key=lambda channel: (
            group_sort_index(display_group(channel)),
            satellite_sort_key(channel),
            -measured_score(channel),
            canonical_display_name(channel).lower(),
        ),
    )



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


def load_source(spec: tuple[str, str, bool]) -> tuple[list[Channel], str | None]:
    group, url, allow_geo = spec
    try:
        return parse_m3u(fetch_text(url), group, allow_geo, source=url), None
    except Exception as exc:
        return [], f"{group}:{url}:{type(exc).__name__}:{str(exc)[:100]}"


def main() -> int:
    source_failures: list[str] = []
    candidates: list[Channel] = []
    previous_history = load_health_history()
    carried_forward_candidates = 0
    existing_easy_channels: list[Channel] = []
    existing_easy_playlist = Path("tv-easy.m3u")
    if existing_easy_playlist.exists():
        existing_easy_channels = parse_m3u(
            existing_easy_playlist.read_text(encoding="utf-8", errors="ignore"),
            "现有订阅",
            False,
            source="carried:tv-easy.m3u",
        )
    existing_playlist = Path("tv.m3u")
    if existing_playlist.exists():
        existing_channels = parse_m3u(
            existing_playlist.read_text(encoding="utf-8", errors="ignore"),
            "现有订阅",
            False,
            source="carried:tv.m3u",
        )
        # Always re-probe the last-known-good URLs even if an upstream index
        # removes them. Curated=True only guarantees inclusion in probe_pool;
        # dead routes still cannot pass the live/video checks.
        for channel in existing_channels:
            channel.curated = True
        candidates.extend(existing_channels)
        carried_forward_candidates = len(existing_channels)
    # Source indexes are independent. Fetch them concurrently, while
    # executor.map preserves SOURCES order for reproducible tie-breaking.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(SOURCE_WORKERS, len(SOURCES))) as executor:
        for parsed_channels, failure in executor.map(load_source, SOURCES):
            candidates.extend(parsed_channels)
            if failure:
                source_failures.append(failure)

    for name, group, url in EXTRAS:
        extinf = f'#EXTINF:-1 group-title="{group}",{name}'
        candidates.append(
            Channel(name, extinf, url, group, False, True, {}, source="curated:extras")
        )

    rejected_non_station = sum(not is_station_like(channel) for channel in candidates)
    raw_source_candidates = Counter(channel.source or "unknown" for channel in candidates)
    candidates = deduplicate(candidates)
    attach_health_history(candidates, previous_history)
    probe_pool = select_probe_pool(candidates)
    print(
        f"candidates={len(candidates)} probe_pool={len(probe_pool)} "
        f"source_workers={SOURCE_WORKERS} probe_workers={PROBE_WORKERS}"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
        future_map = {executor.submit(probe_channel, channel): channel for channel in probe_pool}
        for completed, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            channel = future_map[future]
            try:
                channel.probe = future.result()
            except Exception as exc:
                channel.probe = {"ok": False, "error": type(exc).__name__ + ":" + str(exc)[:80]}
            if completed % 50 == 0:
                print(f"probed={completed}/{len(probe_pool)}")

    # High-availability pass for the entire playlist, not only CCTV/satellites.
    # Every initially healthy route must fetch a second fresh manifest/video
    # segment. One-shot successes remain penalized last-resort fallbacks.
    recheck_targets = [
        channel for channel in probe_pool
        if channel.probe.get("ok")
    ]
    route_recheck_failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
        future_map = {executor.submit(probe_channel, channel): channel for channel in recheck_targets}
        for future in concurrent.futures.as_completed(future_map):
            channel = future_map[future]
            first = dict(channel.probe)
            try:
                second = future.result()
            except Exception as exc:
                second = {"ok": False, "error": type(exc).__name__ + ":" + str(exc)[:80]}
            if not second.get("ok"):
                fallback = dict(first)
                fallback["checks_ok"] = 1
                fallback["recheck_failed"] = True
                fallback["recheck_error"] = str(second.get("error") or "probe_failed")[:100]
                channel.probe = fallback
                route_recheck_failed += 1
                continue
            combined = merge_probe_results(first, second, checks_ok=2)
            combined["checks_ok"] = 2
            combined["recheck_ok"] = True
            channel.probe = combined
    print(
        f"routes_rechecked={len(recheck_targets)} "
        f"route_recheck_failed={route_recheck_failed}"
    )

    # A third fresh manifest/segment pass is reserved for the one-click list.
    # Test every route that cleared the ordinary stable gate so a channel can
    # fall back to another independently hosted URL before disappearing.
    easy_recheck_targets = [
        channel for channel in probe_pool
        if int(channel.probe.get("checks_ok") or 0) >= 2
        and (is_stable(channel) or is_core_acceptable(channel))
    ]
    easy_recheck_failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
        future_map = {
            executor.submit(probe_channel, channel): channel
            for channel in easy_recheck_targets
        }
        for future in concurrent.futures.as_completed(future_map):
            channel = future_map[future]
            first = dict(channel.probe)
            try:
                third = future.result()
            except Exception as exc:
                third = {"ok": False, "error": type(exc).__name__ + ":" + str(exc)[:80]}
            if not third.get("ok"):
                failed = dict(first)
                failed["easy_check_failed"] = True
                failed["easy_check_error"] = str(third.get("error") or "probe_failed")[:100]
                channel.probe = failed
                easy_recheck_failed += 1
                continue
            combined = merge_probe_results(first, third, checks_ok=3)
            combined["easy_recheck_ok"] = True
            channel.probe = combined
    print(
        f"easy_routes_rechecked={len(easy_recheck_targets)} "
        f"easy_route_recheck_failed={easy_recheck_failed}"
    )

    duplicate_core_content_collisions = mark_duplicate_core_content(probe_pool)
    print(
        "duplicate_core_content_collisions="
        + json.dumps(duplicate_core_content_collisions, ensure_ascii=False)
    )

    stable_primary = select_stable(probe_pool)
    easy = restore_existing_family_core(
        select_easy(probe_pool), existing_easy_channels
    )
    easy_keys = {channel_key(channel) for channel in easy}
    easy_missing_cctv = [target for target in CCTV_AUDIT_IDS if target not in easy_keys]
    easy_satellite_names = sorted(
        {
            name
            for channel in easy
            if (name := canonical_mainland_satellite_name(channel))
        }
    )
    easy_missing_satellites = [
        target for target in MAINLAND_SATELLITE_NAMES
        if target not in set(easy_satellite_names)
    ]
    stable = add_cctv5_backups(stable_primary, probe_pool)
    full = select_all(candidates, stable)
    main_playlist = select_main(stable, full)
    history_payload = update_health_history(probe_pool, previous_history)
    healthy = sum(
        1
        for channel in probe_pool
        if channel.probe.get("ok") and not channel.probe.get("duplicate_core_content")
    )
    healthy_by_source = Counter(
        channel.source or "unknown"
        for channel in probe_pool
        if channel.probe.get("ok") and not channel.probe.get("duplicate_core_content")
    )
    geo = sum(1 for channel in probe_pool if channel.probe.get("geo_restricted"))
    cctv5_primary = next(
        (channel for channel in stable if canonical_display_name(channel) == "CCTV-5"),
        None,
    )
    legacy_safety_reasons: list[str] = []
    cctv5_download = float(cctv5_primary.probe.get("segment_mbps") or 0) if cctv5_primary else 0
    cctv5_stream = float(cctv5_primary.probe.get("stream_mbps") or 0) if cctv5_primary else 0
    if (
        cctv5_primary is None
        or not cctv5_primary.probe.get("ok")
        or int(cctv5_primary.probe.get("height") or labelled_height(cctv5_primary)) < 720
        or cctv5_download < max(2.0, cctv5_stream * 1.15)
    ):
        legacy_safety_reasons.append("no verified HD CCTV-5 route with download headroom")
    if len(stable) < MIN_STABLE:
        legacy_safety_reasons.append(
            f"only {len(stable)}/{MIN_STABLE} minimum stable channels (target {TARGET_STABLE})"
        )
    if len(main_playlist) < MIN_MAIN:
        legacy_safety_reasons.append(
            f"only {len(main_playlist)}/{MIN_MAIN} minimum main channels (target {TARGET_MAIN})"
        )
    if len(full) < TARGET_ALL:
        legacy_safety_reasons.append(
            f"only {len(full)}/{TARGET_ALL} all-list channels"
        )
    if len(easy) < MIN_EASY:
        raise SystemExit(
            f"safety stop: only {len(easy)}/{MIN_EASY} triple-checked easy channels "
            f"(target {TARGET_EASY}); existing playlists were not replaced"
        )
    if easy_missing_cctv:
        print(
            "missing_core_diagnostics="
            + json.dumps(
                core_candidate_diagnostics(probe_pool, easy_missing_cctv),
                ensure_ascii=False,
            )
        )
        raise SystemExit(
            "safety stop: family easy-view list is missing required CCTV channels "
            + ",".join(easy_missing_cctv)
            + "; existing playlists were not replaced"
        )
    if easy_missing_satellites:
        print(
            "missing_core_diagnostics="
            + json.dumps(
                core_candidate_diagnostics(probe_pool, easy_missing_satellites),
                ensure_ascii=False,
            )
        )
        raise SystemExit(
            "safety stop: family easy-view list is missing required satellite channels "
            + ",".join(easy_missing_satellites)
            + "; existing playlists were not replaced"
        )

    write_playlist(
        Path("tv-easy.m3u"),
        easy,
        "APTV 无脑稳定版：三轮实测、历史稳定度与 IPv6/IPv4 双栈优选",
    )
    if legacy_safety_reasons:
        print(
            "legacy playlists preserved while family list can update: "
            + "; ".join(legacy_safety_reasons)
        )
    else:
        write_playlist(
            Path("tv.m3u"),
            main_playlist,
            "APTV 约450台：央视卫视实测画质/码率/速度平衡，区域频道实测优先",
        )
        write_playlist(Path("tv-all.m3u"), full, "APTV 完整备用版：频道更多，未全部通过稳定性门槛")
    HISTORY_PATH.write_text(
        json.dumps(history_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    stable_groups = Counter(display_group(channel) for channel in stable)
    main_groups = Counter(display_group(channel) for channel in main_playlist)
    easy_groups = Counter(display_group(channel) for channel in easy)
    stable_core = sum(is_core_channel(channel) for channel in stable)
    easy_core = sum(is_core_channel(channel) for channel in easy)
    easy_core_fallbacks = sum(bool(channel.probe.get("easy_core_fallback")) for channel in easy)
    easy_carried_core_fallbacks = sum(
        bool(channel.probe.get("carried_family_fallback")) for channel in easy
    )
    stable_public_pay = sum(is_public_pay_channel(channel) for channel in stable)
    stable_chinese_oriented = sum(is_chinese_oriented(channel) for channel in stable)
    stable_extinfs = [cleaned_extinf(channel) for channel in stable]
    stable_with_logo = sum('tvg-logo="' in extinf for extinf in stable_extinfs)
    stable_with_epg_id = sum('tvg-id="' in extinf for extinf in stable_extinfs)
    stable_satellite_double_checked = sum(
        display_group(channel) == "卫视台" and int(channel.probe.get("checks_ok") or 1) >= 2
        for channel in stable
    )
    stable_satellite_single_check_fallbacks = sum(
        display_group(channel) == "卫视台" and bool(channel.probe.get("recheck_failed"))
        for channel in stable
    )
    stable_satellite_china_side_fallbacks = sum(
        display_group(channel) == "卫视台" and bool(channel.probe.get("unverified_china_side_fallback"))
        for channel in stable
    )
    core_fallbacks = sum(is_core_channel(channel) and not is_stable(channel) for channel in stable)
    relaxed_fallbacks = sum(not is_stable(channel) for channel in stable)
    stable_heights = Counter()
    for channel in stable:
        height = int(channel.probe.get("height") or labelled_height(channel))
        if height >= 2160:
            stable_heights["2160+"] += 1
        elif height >= 1080:
            stable_heights["1080"] += 1
        elif height >= 720:
            stable_heights["720"] += 1
        elif height and height < 720:
            stable_heights["core_fallback_below_720"] += 1
        else:
            stable_heights["unlabelled_but_probed"] += 1
    errors = Counter(channel.probe.get("error", "") for channel in probe_pool if not channel.probe.get("ok"))
    required_status = {}
    for target in REQUIRED_CORE_IDS:
        tested = [channel for channel in probe_pool if required_core_id(channel) == target]
        chosen = next((channel for channel in stable if required_core_id(channel) == target), None)
        required_status[target] = {
            "tested": len(tested),
            "healthy": sum(bool(channel.probe.get("ok")) for channel in tested),
            "in_stable": bool(chosen),
            "height": int((chosen.probe.get("height") or labelled_height(chosen)) if chosen else 0),
            "mbps": float(chosen.probe.get("segment_mbps") or 0) if chosen else 0,
            "stream_mbps": round(float(chosen.probe.get("stream_mbps") or 0), 2) if chosen else 0,
        }
    all_cctv_status = {}
    for target in CCTV_AUDIT_IDS:
        tested = [channel for channel in probe_pool if channel_key(channel) == target]
        chosen = next((channel for channel in stable if channel_key(channel) == target), None)
        all_cctv_status[target] = {
            "tested": len(tested),
            "healthy": sum(bool(channel.probe.get("ok")) for channel in tested),
            "in_stable": bool(chosen),
            "url": chosen.url if chosen else "",
            "height": int((chosen.probe.get("height") or labelled_height(chosen)) if chosen else 0),
            "mbps": round(float(chosen.probe.get("segment_mbps") or 0), 2) if chosen else 0,
            "stream_mbps": round(float(chosen.probe.get("stream_mbps") or 0), 2) if chosen else 0,
            "checks_ok": int(chosen.probe.get("checks_ok") or 0) if chosen else 0,
        }
    satellite_status = {}
    for target in MAINLAND_SATELLITE_NAMES:
        tested = [
            channel for channel in probe_pool
            if canonical_mainland_satellite_name(channel) == target
        ]
        chosen = next(
            (channel for channel in stable if canonical_mainland_satellite_name(channel) == target),
            None,
        )
        satellite_status[target] = {
            "tested": len(tested),
            "healthy": sum(bool(channel.probe.get("ok")) for channel in tested),
            "in_stable": bool(chosen),
            "url": chosen.url if chosen else "",
            "height": int((chosen.probe.get("height") or labelled_height(chosen)) if chosen else 0),
            "mbps": round(float(chosen.probe.get("segment_mbps") or 0), 2) if chosen else 0,
            "stream_mbps": round(float(chosen.probe.get("stream_mbps") or 0), 2) if chosen else 0,
            "checks_ok": int(chosen.probe.get("checks_ok") or 0) if chosen else 0,
        }
    satellite_group_audit = [
        {
            "name": canonical_display_name(channel),
            "url": channel.url,
            "height": int(channel.probe.get("height") or labelled_height(channel)),
            "mbps": round(float(channel.probe.get("segment_mbps") or 0), 2),
            "stream_mbps": round(float(channel.probe.get("stream_mbps") or 0), 2),
            "checks_ok": int(channel.probe.get("checks_ok") or 1),
        }
        for channel in stable
        if display_group(channel) == "卫视台"
    ]
    cctv5_output_routes = [
        {
            "name": canonical_display_name(channel),
            "url": channel.url,
            "height": int(channel.probe.get("height") or labelled_height(channel)),
            "mbps": round(float(channel.probe.get("segment_mbps") or 0), 2),
            "checks_ok": int(channel.probe.get("checks_ok") or 1),
        }
        for channel in stable
        if channel_key(channel) == "cctv5"
    ]
    pay_focus = re.compile(r"(?:求索|(?<![a-z])chc|newtv|第一剧场|世界地理|风云(?:剧场|足球|音乐))", re.I)
    pay_variants: dict[str, list[Channel]] = defaultdict(list)
    for channel in probe_pool:
        if pay_focus.search(f"{channel.name} {channel.extinf}"):
            pay_variants[channel_key(channel)].append(channel)
    pay_status = {}
    for key, tested in sorted(pay_variants.items()):
        chosen = next((channel for channel in stable if channel_key(channel) == key), None)
        best_ok = max(
            (channel for channel in tested if channel.probe.get("ok")),
            key=measured_score,
            default=None,
        )
        error_counts = Counter(
            channel.probe.get("error", "unknown")
            for channel in tested
            if not channel.probe.get("ok")
        )
        measured = chosen or best_ok
        pay_status[key] = {
            "name": measured.name if measured else tested[0].name,
            "tested": len(tested),
            "healthy": sum(bool(channel.probe.get("ok")) for channel in tested),
            "geo": sum(bool(channel.probe.get("geo_restricted")) for channel in tested),
            "in_stable": bool(chosen),
            "height": int((measured.probe.get("height") or labelled_height(measured)) if measured else 0),
            "mbps": round(float(measured.probe.get("segment_mbps") or 0), 2) if measured else 0,
            "errors": error_counts.most_common(3),
        }

    report_lines = [
        f"generated_utc={GENERATED_AT}",
        f"source_candidates={len(candidates)}",
        f"carried_forward_candidates={carried_forward_candidates}",
        f"rejected_non_station={rejected_non_station}",
        f"probed={len(probe_pool)}",
        f"probe_healthy={healthy}",
        f"geo_restricted={geo}",
        f"routes_rechecked={len(recheck_targets)}",
        f"route_recheck_failed={route_recheck_failed}",
        f"easy_routes_rechecked={len(easy_recheck_targets)}",
        f"easy_route_recheck_failed={easy_recheck_failed}",
        f"easy_channels={len(easy)}",
        f"easy_target={TARGET_EASY}",
        f"easy_minimum={MIN_EASY}",
        f"easy_triple_checked={sum(int(channel.probe.get('checks_ok') or 0) >= 3 for channel in easy)}",
        f"easy_ipv6_dns={sum(bool(channel.probe.get('ipv6_dns')) for channel in easy)}",
        f"easy_dual_stack_dns={sum(bool(channel.probe.get('dual_stack_dns')) for channel in easy)}",
        f"easy_cctv_or_major_satellite={easy_core}",
        f"easy_core_relaxed_fallbacks={easy_core_fallbacks}",
        f"easy_core_carried_fallbacks={easy_carried_core_fallbacks}",
        "easy_missing_cctv=" + json.dumps(easy_missing_cctv, ensure_ascii=False),
        "easy_missing_mainland_satellites=" + json.dumps(easy_missing_satellites, ensure_ascii=False),
        "easy_mainland_satellite_names=" + json.dumps(easy_satellite_names, ensure_ascii=False),
        "legacy_playlists_preserved=" + json.dumps(bool(legacy_safety_reasons)),
        "legacy_safety_reasons=" + json.dumps(legacy_safety_reasons, ensure_ascii=False),
        f"history_routes={len(history_payload.get('routes', {}))}",
        f"stable_satellite_double_checked={stable_satellite_double_checked}",
        f"stable_satellite_single_check_fallbacks={stable_satellite_single_check_fallbacks}",
        f"stable_satellite_china_side_fallbacks={stable_satellite_china_side_fallbacks}",
        f"stable_channels={len(stable)}",
        f"stable_target={TARGET_STABLE}",
        f"stable_minimum={MIN_STABLE}",
        f"main_channels={len(main_playlist)}",
        f"main_target={TARGET_MAIN}",
        f"main_minimum={MIN_MAIN}",
        f"all_channels={len(full)}",
        f"stable_https={sum(channel.url.startswith('https://') for channel in stable)}",
        f"stable_placeholder_relays={sum(is_placeholder_relay(channel) for channel in stable)}",
        f"stable_chinese_groups={sum(channel.group in CHINESE_GROUPS for channel in stable)}",
        f"stable_cctv_or_major_satellite={stable_core}",
        f"stable_public_pay_channels={stable_public_pay}",
        f"stable_with_logo={stable_with_logo}",
        f"stable_with_epg_id={stable_with_epg_id}",
        f"epg_url={EPG_URL}",
        "stable_public_pay_names=" + json.dumps(
            sorted(channel.name for channel in stable if is_public_pay_channel(channel)),
            ensure_ascii=False,
        ),
        "public_pay_status=" + json.dumps(pay_status, ensure_ascii=False, sort_keys=True),
        f"stable_chinese_oriented={stable_chinese_oriented}",
        "source_candidate_counts=" + json.dumps(
            dict(raw_source_candidates.most_common()), ensure_ascii=False
        ),
        "source_probe_healthy=" + json.dumps(
            dict(healthy_by_source.most_common()), ensure_ascii=False
        ),
        f"stable_core_relaxed_fallbacks={core_fallbacks}",
        f"stable_total_relaxed_fallbacks={relaxed_fallbacks}",
        "required_cctv_status=" + json.dumps(required_status, ensure_ascii=False, sort_keys=True),
        "all_cctv_status=" + json.dumps(all_cctv_status, ensure_ascii=False, sort_keys=True),
        "cctv10_candidate_diagnostics=" + json.dumps(
            core_candidate_diagnostics(probe_pool, ["cctv10"]),
            ensure_ascii=False,
        ),
        "duplicate_core_content_collisions=" + json.dumps(
            duplicate_core_content_collisions, ensure_ascii=False
        ),
        "mainland_satellite_status=" + json.dumps(satellite_status, ensure_ascii=False),
        "satellite_group_audit=" + json.dumps(satellite_group_audit, ensure_ascii=False),
        "cctv5_output_routes=" + json.dumps(cctv5_output_routes, ensure_ascii=False),
        "stable_resolution=" + json.dumps(dict(stable_heights), ensure_ascii=False, sort_keys=True),
        "stable_groups=" + json.dumps(dict(stable_groups), ensure_ascii=False, sort_keys=True),
        "main_groups=" + json.dumps(dict(main_groups), ensure_ascii=False, sort_keys=True),
        "easy_groups=" + json.dumps(dict(easy_groups), ensure_ascii=False, sort_keys=True),
        "source_failures=" + json.dumps(source_failures, ensure_ascii=False),
        "top_probe_errors=" + json.dumps(errors.most_common(12), ensure_ascii=False),
    ]
    Path("build-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n".join(report_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
