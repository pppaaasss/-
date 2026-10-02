# CCTV-8、CCTV-11 换源试播（2026-10-02）

用户反馈两台卡顿，并明确授权“可以，你替换一下我去测试”。本次同步四份正式订阅，供电视实际试播。

| 频道 | 原地址 | 新试播地址 | 可核查证据 |
|---|---|---|---|
| CCTV-8 | `http://183.129.255.66:8480/hls/9/index.m3u8` | `http://58.210.139.130:9901/tsfile/live/0008_1.m3u8?key=txiptv&playlive=1&authid=0` | 2026-10-02 07:16:21 北京时间家庭换前复核 QUALIFIED；1920×1080、25 fps，下载 22.514 Mbps、视频 8.005 Mbps |
| CCTV-11 | `http://112.123.243.37:50085/tsfile/live/0012_1.m3u8?key=txiptv&playlive=0&authid=0` | `http://120.40.39.246:352/newlive/live/hls/12/live.m3u8` | 2026-10-01 06:57:32 北京时间家庭检测 QUALIFIED；1920×1080、50 fps，下载 69.591 Mbps、视频 9.236 Mbps；截至授权时已超过36小时资格期，按用户授权人工试播 |

## 证据与发布范围

- CCTV-8 来源：[家庭报告](https://github.com/pppaaasss/-/blob/home-reports/inbox/home-ac86u-8f8908f0fba9/20261001T192532Z-primary-0200-36dc3da24f5e807b.json)，candidate_id `17b8d3b4ed228dcad0169983e519f0ecc9c52e87ac9dd9c38008ad500e6b57ef`。
- CCTV-11 来源：[家庭报告](https://github.com/pppaaasss/-/blob/home-reports/inbox/home-ac86u-8f8908f0fba9/20260930T212102Z-primary-0200-03c2883c808a4520.json)，candidate_id `09f7cd001df4ca47f78a4d0280fa43ff0db14e43ad67b8bdf3fe271830fbc72d`。
- 四份订阅：`tv-core.m3u`、`tv-easy.m3u`、`tv.m3u`、`tv-all.m3u`；每份两台各一个条目，仅变更这两个媒体URL。
- 正式受管频道保持53个；`harvest/home-candidates.json` 的正式清单 SHA256 同步为 `cea21cf365bf0580d4dba902cc00d6767f6c0c8d3106bdd8c95c408d9aaf3c97`。
- 用户对原地址的卡顿反馈写入 `config/home-route-feedback.json`。原有串台、黑屏、主机级否决继续生效。
- 本次未新增直播探测，未延长历史检测资格，未新增任何 good 播放反馈；CCTV-11 为历史源人工试播，不能写成当前复测合格。

## 实际验收

刷新订阅后，分别确认 CCTV-8 是电视剧频道、CCTV-11 是戏曲频道，并观察是否有转圈、停顿、黑屏或声音异常。建议在发生卡顿的时段连续观看10–15分钟；实际反馈再记录到对应精确URL。
