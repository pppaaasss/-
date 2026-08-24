#!/usr/bin/env python3
"""Build APTV playlists with real HLS manifest and media-segment probes.

The main playlist (tv.m3u) favors fast 720p/1080p streams.  A broader
tv-all.m3u is kept as a fallback so that a transient probe failure does not
make a channel disappear completely.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# Keep tv.m3u as the permanent APTV subscription path; only its contents change.
TARGET_STABLE = int(os.getenv("TARGET_STABLE", "600"))
TARGET_ALL = int(os.getenv("TARGET_ALL", "800"))
PROBE_WORKERS = int(os.getenv("PROBE_WORKERS", "44"))
PROBE_TIMEOUT = float(os.getenv("PROBE_TIMEOUT", "9"))
MAX_VARIANTS_PER_CHANNEL = int(os.getenv("MAX_VARIANTS_PER_CHANNEL", "8"))
MAX_PROBE_BYTES = 768 * 1024
TODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

DEFAULT_UA = "Mozilla/5.0 (AppleTV; APTV playlist health-check/2.0)"

# Chinese-first 400-channel target distribution. Missing groups are filled by
# the fastest remaining healthy streams, so a weak regional source cannot stop
# the list from reaching the requested size.
GROUP_TARGETS = {
    "大陆": 235,
    "中文综合": 50,
    "中文纪录": 30,
    "中文电影": 30,
    "中文付费": 50,
    "香港": 35,
    "澳门": 5,
    "台湾": 45,
    "新加坡": 15,
    "马来西亚": 15,
    "日本": 10,
    "韩国": 10,
    "纪录片": 15,
    "电影": 15,
    "新闻": 10,
    "娱乐": 10,
    "体育": 10,
    "少儿": 5,
    "音乐": 5,
}

GROUP_ORDER = {name: i for i, name in enumerate(GROUP_TARGETS)}
CHINESE_GROUPS = {
    "大陆", "中文综合", "中文纪录", "中文电影", "中文付费", "香港", "澳门", "台湾", "新加坡", "马来西亚"
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
    ("韩国", "https://iptv-org.github.io/iptv/countries/kr.m3u", True),
    ("纪录片", "https://iptv-org.github.io/iptv/categories/documentary.m3u", False),
    ("电影", "https://iptv-org.github.io/iptv/categories/movies.m3u", False),
    ("新闻", "https://iptv-org.github.io/iptv/categories/news.m3u", False),
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
    ("韩国", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_korea.m3u8", True),

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
    ("大陆", "https://raw.githubusercontent.com/jura00/vms/main/hd.m3u8", False),
    # Small HTTPS relay list with currently maintained CHC linear channels.
    ("中文付费", "https://cdn.jsdelivr.net/gh/jyoketsu/tv@main/live.txt", False),
]

# Curated fallbacks.  The five required CCTV stations use multiple independent
# routes; all variants are probed and only the fastest healthy URL reaches
# tv.m3u.  Documentary/movie requests remain available in tv-all.m3u.
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
    # CCTV-5 must remain 1080p. These are video-CDN paths (not the AAC-only
    # /audio/ feed that previously produced a black screen in APTV).
    ("CCTV-5 体育 1080p 海外镜像", "大陆", "http://38.75.136.137:98/gslb/dsdqpub/cctv5hd.m3u8?auth=testpub"),
    ("CCTV-5 体育 1080p 海外镜像", "大陆", "http://207.56.13.146:81/cdnlive/cctv5.m3u8"),
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
    "http://207.56.13.146:81/cdnlive/cctv5.m3u8",
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
    # Public lists label this Zhejiang Shaoxing/Shengzhou local feed as
    # Shandong Satellite TV. Keep it out even when its HLS probe succeeds.
    "http://l.cztvcloud.com/channels/lantian/SXshengzhou1/720p.m3u8",
    # User-tested: valid H.264 video but sustained throughput is too unstable
    # for CCTV-5, despite passing short remote probes.
    "http://gmxw.7766.org:808/hls/93/index.m3u8",
}
REQUIRED_CORE_IDS = ("cctv5", "cctv5plus", "cctv9", "cctv12", "cctv16")
CHINA_SIDE_FALLBACK_IDS = {"cctv1", "cctv2", "cctv5", "cctv8", "cctv11", "湖南卫视", "山东卫视"}

MAINLAND_SATELLITE_NAMES = (
    "北京卫视", "东方卫视", "湖南卫视", "浙江卫视", "江苏卫视", "广东卫视", "深圳卫视",
    "安徽卫视", "山东卫视", "河南卫视", "湖北卫视", "辽宁卫视", "黑龙江卫视", "四川卫视",
    "重庆卫视", "天津卫视", "河北卫视", "江西卫视", "广西卫视", "贵州卫视", "云南卫视",
    "陕西卫视", "山西卫视", "吉林卫视", "内蒙古卫视", "新疆卫视", "西藏卫视", "青海卫视",
    "甘肃卫视", "宁夏卫视", "海南卫视", "东南卫视", "延边卫视", "海峡卫视", "兵团卫视",
    "安多卫视", "农林卫视", "三沙卫视",
)
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


def parse_m3u(text: str, group: str, allow_geo: bool) -> list[Channel]:
    """Parse ordinary M3U plus common Chinese name,url TXT playlists."""
    channels: list[Channel] = []
    extinf: str | None = None

    def append_channel(name: str, item_extinf: str, url: str) -> None:
        clean_url = url.split("$", 1)[0].strip()
        if not clean_url.startswith(("http://", "https://")):
            return
        low = f"{name} {item_extinf}".lower()
        assigned_group = group
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
            Channel(name, item_extinf, clean_url, assigned_group, allow_geo, False, parse_headers(item_extinf))
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


def channel_key(channel: Channel) -> str:
    """Canonical key used only after alternate URLs have been speed-tested."""
    visible_name = normalized_name(channel.name)
    # CCTV-4 (Chinese International) and CCTV-4K are separate channels, while
    # spelling variants such as CCTV4K / CCTV-4K HD are one channel.
    if is_cctv4k_label(channel):
        return "cctv4k"
    satellite_name = canonical_mainland_satellite_name(channel)
    if satellite_name:
        return satellite_name
    # Prefer the visible CCTV name. Some imported lists use numeric tvg-id="5"
    # or tvg-id="6", which must not split CCTV-5/5+ into unrelated keys.
    required = required_core_id(channel)
    if required:
        return required
    cctv_number = re.search(r"cctv[\s_-]*0?(\d{1,2})(?!\d)", visible_name, re.I)
    if cctv_number and 1 <= int(cctv_number.group(1)) <= 17:
        return f"cctv{int(cctv_number.group(1))}"
    # Lists often assign inconsistent tvg-id values to the same station. Use
    # the cleaned visible label for every language so alternate routes cannot
    # inflate the 600-channel count.
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


def is_core_channel(channel: Channel) -> bool:
    visible = normalized_name(channel.name)
    return bool(re.search(r"\bcctv[\s_-]*\d+", visible, re.I)) or canonical_mainland_satellite_name(channel) is not None


def is_placeholder_relay(channel: Channel) -> bool:
    host = (urllib.parse.urlsplit(channel.url).hostname or "").lower()
    return host in PLACEHOLDER_RELAY_HOSTS or host == "freetv.fun" or host.endswith(".freetv.fun")


def is_station_like(channel: Channel) -> bool:
    """Reject radio, movies, episodes and other entries that are not linear TV."""
    name_low = channel.name.lower()
    low = f"{channel.name} {channel.extinf}".lower()
    if channel.url.rstrip("/") in {url.rstrip("/") for url in MISLABELLED_STREAM_URLS}:
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
    identity = f"{channel.name} {channel.extinf}".lower()
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
    if not rows:
        return None
    # Lock the health test to the best <=1080p representation.  APTV still gets
    # the original master URL and can adapt down if the user's route fluctuates.
    normal = [row for row in rows if 720 <= row[1] <= 1080 and (not row[2] or row[2] <= 14_000_000)]
    if normal:
        return max(normal, key=lambda row: (row[1], row[0], row[2]))
    under = [row for row in rows if row[1] and row[1] <= 1080]
    if under:
        return max(under, key=lambda row: (row[1], row[0], row[2]))
    return min(rows, key=lambda row: (row[1] or 99999, row[2] or 999999999))


def first_media_uri(text: str, base_url: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")) or not urllib.parse.urlsplit(line).scheme:
            return urllib.parse.urljoin(base_url, line)
    return None


def timed_read(url: str, headers: dict[str, str], limit: int, timeout: float) -> tuple[bytes, float, str]:
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    request_headers.update(headers)
    request_headers["Range"] = f"bytes=0-{limit - 1}"
    req = urllib.request.Request(url, headers=request_headers)
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        final_url = response.geturl()
        data = response.read(limit)
    return data, max(time.monotonic() - started, 0.001), final_url


def probe_once(channel: Channel, use_declared_headers: bool) -> dict:
    headers = channel.headers if use_declared_headers else {}
    manifest_bytes, manifest_seconds, final_url = timed_read(channel.url, headers, 512 * 1024, PROBE_TIMEOUT)
    text = manifest_bytes.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        raise ValueError("not_hls")

    rows = variant_rows(text, final_url)
    selected_variant = choose_variant(rows)
    width = height = bandwidth = 0
    media_url = final_url
    media_text = text
    media_seconds = manifest_seconds
    if selected_variant:
        width, height, bandwidth, media_url = selected_variant
        media_bytes, media_seconds, media_url = timed_read(media_url, headers, 512 * 1024, PROBE_TIMEOUT)
        media_text = media_bytes.decode("utf-8", "ignore")
        if "#EXTM3U" not in media_text:
            raise ValueError("bad_variant")

    upper_media = media_text.upper()
    if "#EXT-X-ENDLIST" in upper_media or re.search(
        r"#EXT-X-PLAYLIST-TYPE\s*:\s*VOD", upper_media
    ):
        raise ValueError("vod_playlist")

    segment_url = first_media_uri(media_text, media_url)
    if not segment_url:
        raise ValueError("no_live_segment")
    segment, segment_seconds, _ = timed_read(segment_url, headers, MAX_PROBE_BYTES, PROBE_TIMEOUT)
    if len(segment) < 16 * 1024:
        raise ValueError("short_segment")
    speed_mbps = len(segment) * 8 / segment_seconds / 1_000_000
    return {
        "ok": True,
        "manifest_s": round(manifest_seconds + (media_seconds if selected_variant else 0), 3),
        "segment_mbps": round(speed_mbps, 2),
        "segment_bytes": len(segment),
        "width": width,
        "height": height,
        "bandwidth": bandwidth,
        "header_required": use_declared_headers,
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


def is_stable(channel: Channel) -> bool:
    if is_placeholder_relay(channel):
        return False
    probe = channel.probe
    if probe.get("geo_restricted"):
        # Official regional CDN streams can be valid behind the user's HK/JP/SG
        # exit even when GitHub's US runner receives a geo error.
        return channel.allow_geo and channel.static_score >= 55 and labelled_height(channel) >= 720
    if not probe.get("ok") or probe.get("header_required"):
        return False
    height = probe.get("height") or labelled_height(channel)
    speed = float(probe.get("segment_mbps") or 0)
    latency = float(probe.get("manifest_s") or 99)
    bandwidth_mbps = float(probe.get("bandwidth") or 0) / 1_000_000
    required_speed = max(2.2, bandwidth_mbps * 1.25)
    if height and height < 720:
        return False
    if latency > 5.0 or speed < required_speed:
        return False
    parsed = urllib.parse.urlsplit(channel.url)
    raw_ip = bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parsed.hostname or ""))
    if channel.url.startswith("http://") and (raw_ip or parsed.port not in (None, 80)):
        return speed >= 5.0 and latency <= 3.5
    return True


def is_core_acceptable(channel: Channel) -> bool:
    """Slightly relaxed floor for must-have CCTV and provincial channels."""
    if not is_core_channel(channel) or is_placeholder_relay(channel):
        return False
    probe = channel.probe
    if probe.get("geo_restricted"):
        return False
    if not probe.get("ok") or probe.get("header_required"):
        return False
    height = int(probe.get("height") or labelled_height(channel))
    speed = float(probe.get("segment_mbps") or 0)
    latency = float(probe.get("manifest_s") or 99)
    # CCTV-5+ currently has public 576i fallbacks; accept those only for the
    # core guarantee, while ordinary channels still require the HD threshold.
    return (not height or height >= 540) and speed >= 1.2 and latency <= 8.0


def measured_score(channel: Channel) -> float:
    score = channel.static_score
    probe = channel.probe
    if probe.get("geo_restricted"):
        return score - 25
    if not probe.get("ok"):
        return -9999
    speed = min(float(probe.get("segment_mbps") or 0), 40)
    latency = float(probe.get("manifest_s") or 10)
    height = int(probe.get("height") or labelled_height(channel))
    score += speed * 2.2 - latency * 7
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
    return score


def deduplicate(channels: Iterable[Channel]) -> list[Channel]:
    best_by_url: dict[str, Channel] = {}
    for channel in channels:
        if not is_station_like(channel):
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
        limit = 24 if required_core_id(items[0]) else MAX_VARIANTS_PER_CHANNEL
        result.extend(items[:limit])
    return result


def select_probe_pool(channels: list[Channel]) -> list[Channel]:
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in channels:
        grouped[channel.group].append(channel)
    pool: list[Channel] = []
    for group, items in grouped.items():
        quota = GROUP_TARGETS.get(group, 5)
        by_channel: dict[str, list[Channel]] = defaultdict(list)
        for item in items:
            by_channel[channel_key(item)].append(item)
        for variants in by_channel.values():
            variants.sort(key=lambda item: item.static_score, reverse=True)

        # Probe up to 24 independent routes for each required CCTV station
        # before the ordinary breadth-first pool. Domestic CDN routes often
        # reject a US GitHub runner while another route remains healthy.
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
                for channel in variants[:MAX_VARIANTS_PER_CHANNEL]:
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
    # Keep the fastest measured URL for each actual channel.  URL alternatives
    # are useful during probing but must not become duplicate APTV tiles.
    best: dict[str, Channel] = {}
    for channel in channels:
        if not (is_stable(channel) or is_core_acceptable(channel)):
            continue
        key = channel_key(channel)
        existing = best.get(key)
        if existing is None or measured_score(channel) > measured_score(existing):
            best[key] = channel

    # CCTV-5 is refreshed automatically: a preferred overseas route may stay
    # primary only while its manifest and video segment pass this run. If it
    # expires, the best double-checked 1080p candidate takes over immediately.
    cctv5_verified = [
        channel for channel in channels
        if channel_key(channel) == "cctv5"
        and channel.probe.get("ok")
        and not channel.probe.get("header_required")
        and int(channel.probe.get("height") or labelled_height(channel)) >= 1080
        and "/audio/" not in urllib.parse.urlsplit(channel.url).path.lower()
        and not is_placeholder_relay(channel)
    ]
    if cctv5_verified:
        best["cctv5"] = max(
            cctv5_verified,
            key=lambda channel: (
                int(channel.probe.get("checks_ok") or 1) >= 2,
                channel.url == CCTV5_PREFERRED_1080_URLS[0],
                channel.url in CCTV5_PREFERRED_1080_URLS,
                not any(
                    token in (urllib.parse.urlsplit(channel.url).hostname or "").lower()
                    for token in CCTV5_OPERATOR_HINTS
                ),
                min(float(channel.probe.get("segment_mbps") or 0), 30),
                -float(channel.probe.get("manifest_s") or 99),
                measured_score(channel),
            ),
        )

    # Keep the fastest truly playable route for every requested CCTV station
    # and mainland satellite channel even if it narrowly misses the general
    # speed floor. Ordinary channels remain subject to the strict threshold.
    fallback_keys = set(REQUIRED_CORE_IDS)
    fallback_keys.update(
        channel_key(channel)
        for channel in channels
        if channel.group == "大陆" and is_core_channel(channel)
    )
    # Publicly shared linear pay channels get the same real-segment fallback:
    # they may miss the general speed floor, but dead/VOD/header-only routes do
    # not pass because probe.ok is mandatory below.
    fallback_keys.update(
        channel_key(channel) for channel in channels if is_public_pay_channel(channel)
    )
    for key in fallback_keys:
        if key in best:
            continue
        fallback = max(
            (
                channel for channel in channels
                if channel_key(channel) == key
                and not is_placeholder_relay(channel)
                and channel.probe.get("ok")
                and not channel.probe.get("header_required")
            ),
            key=measured_score,
            default=None,
        )
        if fallback is not None:
            best[key] = fallback

    # The overseas runner cannot consistently reach several domestic CDN/IPTV
    # routes. Keep one current curated China-side route for these priority
    # stations only when no segment-verified route exists in this build.
    for key in CHINA_SIDE_FALLBACK_IDS:
        if key in best:
            continue
        # For CCTV-5, only a labelled 1080p overseas mirror/public edge CDN is
        # acceptable as an unverified primary. Operator and 720p stay behind.
        if key == "cctv5":
            edge_fallback = max(
                (
                    channel for channel in channels
                    if channel_key(channel) == key
                    and channel.curated
                    and (
                        channel.url in CCTV5_PREFERRED_1080_URLS
                        or any(
                            token in (urllib.parse.urlsplit(channel.url).hostname or "").lower()
                            for token in CCTV5_EDGE_HOSTS
                        )
                    )
                    and labelled_height(channel) >= 1080
                    and not is_placeholder_relay(channel)
                ),
                key=channel_static_score,
                default=None,
            )
            if edge_fallback is not None:
                edge_fallback.probe["unverified_edge_fallback"] = True
                best[key] = edge_fallback
                continue
        fallback = max(
            (
                channel for channel in channels
                if channel_key(channel) == key
                and channel.curated
                and not is_placeholder_relay(channel)
            ),
            key=channel_static_score,
            default=None,
        )
        if fallback is not None:
            fallback.probe["unverified_china_side_fallback"] = True
            best[key] = fallback

    eligible = list(best.values())
    grouped: dict[str, list[Channel]] = defaultdict(list)
    for channel in eligible:
        grouped[channel.group].append(channel)
    for items in grouped.values():
        items.sort(key=measured_score, reverse=True)

    selected: list[Channel] = []
    selected_urls: set[str] = set()
    selected_keys: set[str] = set()

    # Guarantee every playable CCTV/major provincial station before filling
    # entertainment categories. Failed and dead URLs are still excluded.
    core = sorted((channel for channel in eligible if is_core_channel(channel)), key=measured_score, reverse=True)
    for channel in core:
        key = channel_key(channel)
        if key in selected_keys:
            continue
        selected.append(channel)
        selected_urls.add(channel.url)
        selected_keys.add(key)

    for group, quota in GROUP_TARGETS.items():
        already = sum(channel.group == group for channel in selected)
        for channel in grouped.get(group, []):
            if already >= quota:
                break
            key = channel_key(channel)
            if key in selected_keys:
                continue
            selected.append(channel)
            selected_urls.add(channel.url)
            selected_keys.add(key)
            already += 1

    if len(selected) < TARGET_STABLE:
        overflow = sorted(
            (channel for channel in eligible if channel.url not in selected_urls and channel_key(channel) not in selected_keys),
            key=measured_score,
            reverse=True,
        )
        for channel in overflow[: TARGET_STABLE - len(selected)]:
            selected.append(channel)
            selected_urls.add(channel.url)
            selected_keys.add(channel_key(channel))

    # If the strict HD/speed floor finishes just below the requested count,
    # admit only successfully probed live stations through a conservative
    # secondary floor. VOD playlists were already rejected during probing.
    if len(selected) < TARGET_STABLE:
        relaxed_best: dict[str, Channel] = {}
        for channel in channels:
            key = channel_key(channel)
            probe = channel.probe
            height = int(probe.get("height") or labelled_height(channel))
            chinese = is_chinese_oriented(channel)
            min_speed = 0.8 if chinese else 1.5
            max_latency = 10.0 if chinese else 6.0
            if (
                key in selected_keys
                or channel.url in selected_urls
                or is_placeholder_relay(channel)
                or not probe.get("ok")
                or probe.get("geo_restricted")
                or probe.get("header_required")
                or (height and height < 720)
                or float(probe.get("segment_mbps") or 0) < min_speed
                or float(probe.get("manifest_s") or 99) > max_latency
            ):
                continue
            existing = relaxed_best.get(key)
            if existing is None or measured_score(channel) > measured_score(existing):
                relaxed_best[key] = channel
        relaxed = sorted(
            relaxed_best.values(),
            key=lambda channel: (is_chinese_oriented(channel), measured_score(channel)),
            reverse=True,
        )
        selected.extend(relaxed[: TARGET_STABLE - len(selected)])
    return selected[:TARGET_STABLE]


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
            min(float(channel.probe.get("segment_mbps") or 0), 30),
            -float(channel.probe.get("manifest_s") or 99),
            measured_score(channel),
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
            )
        )
        used_hosts.add(host)
        if len(backups) >= count:
            break
    if not backups:
        return stable

    # Preserve the requested 600-channel size by replacing the lowest-scoring
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


def select_all(channels: list[Channel], stable: list[Channel]) -> list[Channel]:
    selected = list(stable)
    urls = {channel.url for channel in selected}
    keys = {channel_key(channel) for channel in selected}
    curated = [
        channel for channel in channels
        if channel.curated
        and not is_placeholder_relay(channel)
        and channel.url not in urls
        and channel_key(channel) not in keys
    ]
    for channel in curated:
        selected.append(channel)
        urls.add(channel.url)
        keys.add(channel_key(channel))
    best_remainder: dict[str, Channel] = {}
    for channel in channels:
        key = channel_key(channel)
        if channel.url in urls or key in keys or is_placeholder_relay(channel):
            continue
        existing = best_remainder.get(key)
        rank = (is_stable(channel), measured_score(channel), channel.static_score)
        if existing is None or rank > (is_stable(existing), measured_score(existing), existing.static_score):
            best_remainder[key] = channel
    remainder = sorted(best_remainder.values(), key=lambda channel: (is_stable(channel), measured_score(channel), channel.static_score), reverse=True)
    selected.extend(remainder[: max(0, TARGET_ALL - len(selected))])
    return selected[:TARGET_ALL]


def display_group(channel: Channel) -> str:
    """Collapse the old Mainland bucket into the two APTV groups requested."""
    identity_extinf = re.sub(r'group-title="[^"]*"', "", channel.extinf, flags=re.I)
    identity = f"{channel.name} {identity_extinf}".lower()
    # CCTV/CGTN can arrive through language/category feeds instead of the old
    # Mainland feed, so classify them independently of their imported group.
    if re.search(r"(?:cctv|cgtn|央视)", identity, re.I):
        return "卫视台"
    if channel.group in {"大陆", "中文综合"} and canonical_mainland_satellite_name(channel):
        return "卫视台"
    if channel.group == "大陆":
        return "中文综合"
    return channel.group


def canonical_display_name(channel: Channel) -> str:
    if channel.display_override:
        return channel.display_override
    key = channel_key(channel)
    if key == "cctv4k":
        return "CCTV-4K"
    if key == "cctv5plus":
        return "CCTV-5+"
    numbered = re.fullmatch(r"cctv(\d{1,2})", key)
    if numbered:
        return f"CCTV-{int(numbered.group(1))}"
    satellite_name = canonical_mainland_satellite_name(channel)
    if satellite_name:
        return satellite_name
    return channel.name


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
    output_order = {"卫视台": 0, "中文综合": 1}
    return sorted(
        channels,
        key=lambda channel: (
            output_order.get(display_group(channel), GROUP_ORDER.get(display_group(channel), 99) + 2),
            satellite_sort_key(channel),
            -measured_score(channel),
            canonical_display_name(channel).lower(),
        ),
    )


def write_playlist(path: Path, channels: list[Channel], description: str) -> None:
    lines = [
        "#EXTM3U",
        f"# {description}",
        f"# generated_utc={TODAY}",
        f"# channels={len(channels)}",
    ]
    for channel in sort_channels(channels):
        lines.extend((cleaned_extinf(channel), channel.url))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    source_failures: list[str] = []
    candidates: list[Channel] = []
    for group, url, allow_geo in SOURCES:
        try:
            candidates.extend(parse_m3u(fetch_text(url), group, allow_geo))
        except Exception as exc:  # keep other source lists usable
            source_failures.append(f"{group}:{type(exc).__name__}:{str(exc)[:100]}")

    for name, group, url in EXTRAS:
        extinf = f'#EXTINF:-1 group-title="{group}",{name}'
        candidates.append(Channel(name, extinf, url, group, False, True, {}))

    rejected_non_station = sum(not is_station_like(channel) for channel in candidates)
    candidates = deduplicate(candidates)
    probe_pool = select_probe_pool(candidates)
    print(f"candidates={len(candidates)} probe_pool={len(probe_pool)} workers={PROBE_WORKERS}")
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

    # High-availability pass for the complete CCTV/CGTN/provincial group.
    # A route must fetch a second fresh manifest/media segment after the broad
    # scan. A one-shot success remains a penalized last-resort fallback so a
    # transient second timeout does not delete an otherwise playable station.
    recheck_targets = [
        channel for channel in probe_pool
        if display_group(channel) == "卫视台" and channel.probe.get("ok")
    ]
    satellite_recheck_failed = 0
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
                satellite_recheck_failed += 1
                continue
            combined = dict(first)
            combined["checks_ok"] = 2
            combined["recheck_ok"] = True
            combined["segment_mbps"] = min(
                float(first.get("segment_mbps") or 0),
                float(second.get("segment_mbps") or 0),
            )
            combined["manifest_s"] = max(
                float(first.get("manifest_s") or 0),
                float(second.get("manifest_s") or 0),
            )
            first_height = int(first.get("height") or 0)
            second_height = int(second.get("height") or 0)
            combined["height"] = min(
                (height for height in (first_height, second_height) if height),
                default=0,
            )
            channel.probe = combined
    print(
        f"satellite_rechecked={len(recheck_targets)} "
        f"satellite_recheck_failed={satellite_recheck_failed}"
    )

    stable = add_cctv5_backups(select_stable(probe_pool), probe_pool)
    full = select_all(candidates, stable)
    healthy = sum(1 for channel in probe_pool if channel.probe.get("ok"))
    geo = sum(1 for channel in probe_pool if channel.probe.get("geo_restricted"))
    cctv5_primary = next(
        (channel for channel in stable if canonical_display_name(channel) == "CCTV-5"),
        None,
    )
    if (
        cctv5_primary is None
        or not cctv5_primary.probe.get("ok")
        or int(cctv5_primary.probe.get("height") or labelled_height(cctv5_primary)) < 1080
    ):
        raise SystemExit(
            "safety stop: no verified 1080p CCTV-5 primary; existing tv.m3u was not replaced"
        )
    if len(stable) < min(120, TARGET_STABLE):
        raise SystemExit(f"safety stop: only {len(stable)} stable channels; existing tv.m3u was not replaced")

    write_playlist(Path("tv.m3u"), stable, "APTV 高清稳定版：实测 HLS 清单与视频分片；1080p/720p 优先")
    write_playlist(Path("tv-all.m3u"), full, "APTV 完整备用版：频道更多，未全部通过稳定性门槛")

    stable_groups = Counter(display_group(channel) for channel in stable)
    stable_core = sum(is_core_channel(channel) for channel in stable)
    stable_public_pay = sum(is_public_pay_channel(channel) for channel in stable)
    stable_chinese_oriented = sum(is_chinese_oriented(channel) for channel in stable)
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
        }
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
        f"generated_utc={TODAY}",
        f"source_candidates={len(candidates)}",
        f"rejected_non_station={rejected_non_station}",
        f"probed={len(probe_pool)}",
        f"probe_healthy={healthy}",
        f"geo_restricted={geo}",
        f"satellite_rechecked={len(recheck_targets)}",
        f"satellite_recheck_failed={satellite_recheck_failed}",
        f"stable_satellite_double_checked={stable_satellite_double_checked}",
        f"stable_satellite_single_check_fallbacks={stable_satellite_single_check_fallbacks}",
        f"stable_satellite_china_side_fallbacks={stable_satellite_china_side_fallbacks}",
        f"stable_channels={len(stable)}",
        f"all_channels={len(full)}",
        f"stable_https={sum(channel.url.startswith('https://') for channel in stable)}",
        f"stable_placeholder_relays={sum(is_placeholder_relay(channel) for channel in stable)}",
        f"stable_chinese_groups={sum(channel.group in CHINESE_GROUPS for channel in stable)}",
        f"stable_cctv_or_major_satellite={stable_core}",
        f"stable_public_pay_channels={stable_public_pay}",
        "stable_public_pay_names=" + json.dumps(
            sorted(channel.name for channel in stable if is_public_pay_channel(channel)),
            ensure_ascii=False,
        ),
        "public_pay_status=" + json.dumps(pay_status, ensure_ascii=False, sort_keys=True),
        f"stable_chinese_oriented={stable_chinese_oriented}",
        f"stable_core_relaxed_fallbacks={core_fallbacks}",
        f"stable_total_relaxed_fallbacks={relaxed_fallbacks}",
        "required_cctv_status=" + json.dumps(required_status, ensure_ascii=False, sort_keys=True),
        "cctv5_output_routes=" + json.dumps(cctv5_output_routes, ensure_ascii=False),
        "stable_resolution=" + json.dumps(dict(stable_heights), ensure_ascii=False, sort_keys=True),
        "stable_groups=" + json.dumps(dict(stable_groups), ensure_ascii=False, sort_keys=True),
        "source_failures=" + json.dumps(source_failures, ensure_ascii=False),
        "top_probe_errors=" + json.dumps(errors.most_common(12), ensure_ascii=False),
    ]
    Path("build-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n".join(report_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
