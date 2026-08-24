# 傻子电视台 · APTV 订阅

给 APTV（iPhone、iPad、Apple TV、Mac）使用的自动维护电视直播清单。

## 订阅地址

- 高清稳定版（推荐，400 台）：`https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u`
- 完整备用版（650 台）：`https://raw.githubusercontent.com/pppaaasss/-/master/tv-all.m3u`

## “高清稳定版”怎么选

GitHub Actions 每天北京时间/新加坡时间 00:17 和 12:17 自动重建：

1. 汇总 iptv-org、vbskycn、best-fan、HerbertHe/ibert、Free-TV、CCSH、Suxuang、BurningC4 等公开清单，并接入 `live.zbds.top` 等站外在线源；重点扩充中国大陆、港台、中文地方台、影视和纪录片。
2. 去掉购物台、MPD 和明显低价值频道；`rtp://239.x` 一类只能在特定运营商局域网使用的组播源不会收录。
3. 普通频道最多保留 8 条候选 URL；CCTV-5、5+、9、12、16 各保留并优先测试最多 24 条线路，省卫视也会测试多条备线。
4. 实际请求 HLS 主清单、720p/1080p 变体清单和一个视频分片，测首开耗时与分片下载速度后选最快的一条；主清单按中文优先配额扩展到 400 台，并继续优先 1080p/720p、HTTPS 和 CDN。
5. 完整备用版保留更多频道；求索、CHC 等指定频道即使当天未通过稳定性门槛也不会从备用版消失。

CCTV 与主要省级卫视使用核心保底规则：CCTV-5、CCTV-5+、CCTV-9、CCTV-12、CCTV-16 分别独立识别；所有成功拉到真实视频分片的大陆卫视优先进入稳定版，不受普通分组配额限制。同名卫视会合并为一个 APTV 频道，后台多线路只用于竞争最快地址。普通高清门槛不通过时，核心频道可采用已经完成分片验证、但速度或清晰度略低的保底源；完全打不开的死源仍不会写入稳定版。

`build-report.txt` 会记录每次构建的候选数、实测通过数、地区限制数、主清单中文分组数量、占位中转数量，以及这 5 个指定 CCTV 台各自的候选数、可播数、清晰度和速度。`t.freetv.fun`、`epg.pw` 等会返回“信号中断”画面的中转源不会进入任何输出播放单。需要注意：测速发生在 GitHub Actions 线路，最终体验仍会受 Apple TV 所用代理出口和晚高峰影响。
