# 傻子电视台 · APTV 订阅

给 APTV（iPhone、iPad、Apple TV、Mac）使用的自动维护电视直播清单。

## 订阅地址

- 高清稳定版（推荐，约 200 台）：`https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u`
- 完整备用版（约 320 台）：`https://raw.githubusercontent.com/pppaaasss/-/master/tv-all.m3u`

## “高清稳定版”怎么选

GitHub Actions 每天北京时间/新加坡时间 00:17 和 12:17 自动重建：

1. 汇总 iptv-org 的中国大陆、香港、澳门、台湾、新加坡、马来西亚、日本、韩国及纪录片、电影等公开清单。
2. 去掉购物台、重复频道、MPD 和明显低价值频道。
3. 实际请求 HLS 主清单、720p/1080p 变体清单和一个视频分片，测首开耗时与分片下载速度。
4. 主清单优先 1080p/720p、HTTPS、CDN 和中文频道；低清、慢速、需要特殊请求头、打不开的源不进入主清单。
5. 完整备用版保留更多频道；求索、CHC 等指定频道即使当天未通过稳定性门槛也不会从备用版消失。

`build-report.txt` 会记录每次构建的候选数、实测通过数、地区限制数和主清单分组数量。需要注意：测速发生在 GitHub Actions 线路，最终体验仍会受 Apple TV 所用代理出口和晚高峰影响。
