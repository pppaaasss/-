# 湖南卫视卡顿换源试播（2026-10-03）

2026-10-03 19:01:17（北京时间），用户反馈湖南卫视偶尔卡顿。查到一条曾通过家庭检测的历史备用，并说明证据已过期；用户于19:04:11回复“可以”，授权换源实际试播。

## 本轮地址

| 订阅 | 原地址 | 新试播地址 |
|---|---|---|
| `tv-core.m3u` | `http://183.94.146.79:9901/tsfile/live/0128_2.m3u8?key=txiptv&playlive=1&authid=0` | `http://192.151.150.154/live/hnwshd.m3u8` |
| `tv-easy.m3u` | `http://hlsal-ldvt.qing.mgtv.com/nn_live/nn_x64/Y2RuZXhfaWQ9YWxfaGxzX2xkdnQmZT02OTE0NjA0JnY9MSZpZD1ITldTWkdTVCZzPTcwN2RiYTc2YzJjNmJmMTQ4MmUyZGYzOWU2NWM3YWFi/HNWSZGST.m3u8` | `http://192.151.150.154/live/hnwshd.m3u8` |
| `tv.m3u` | `http://183.94.146.79:9901/tsfile/live/0128_2.m3u8?key=txiptv&playlive=1&authid=0` | `http://192.151.150.154/live/hnwshd.m3u8` |
| `tv-all.m3u` | `http://183.94.146.79:9901/tsfile/live/0128_2.m3u8?key=txiptv&playlive=1&authid=0` | `http://192.151.150.154/live/hnwshd.m3u8` |

日常精简版的湖南卫视此前使用独立芒果地址；本次四份订阅统一为用户授权的历史候选。每份湖南卫视各一个条目，仅修改该频道媒体URL，条目顺序、元数据和频道数量保留。

## 历史检测依据

- [家庭报告](https://github.com/pppaaasss/-/blob/home-reports/inbox/home-ac86u-8f8908f0fba9/20260928T183931Z-primary-0200-e584f0d1a427dc23.json)，candidate_id `a3c7b7f047a6efaeee49073742912604ac47521868c934537d7b6ef69dc81874`。
- 通过时间：2026-09-29 10:47:12 北京时间（UTC `2026-09-29T02:47:12Z`）。
- 当时 qualification 为 QUALIFIED，H.264、1920×1080、50 fps。
- 下载速度最低 12.185 Mbps，视频码率 3.598 Mbps，下载余量约3.386倍。
- 截至本轮授权已经超过36小时有效期；本次属于用户授权的历史源人工试播，没有新的媒体探测，没有延长资格，也没有新增 good 播放反馈。
- 原主地址的“偶尔卡顿”反馈写入 `config/home-route-feedback.json`，仅归属用户实际观看的精确地址；不将该症状套用到此前精简版芒果地址。

## 发布与验证

四份订阅 `tv-core.m3u`、`tv-easy.m3u`、`tv.m3u`、`tv-all.m3u` 同步换源。正式受管频道保持53个，`harvest/home-candidates.json` 的正式清单 SHA256 更新为 `f2defc19412d92d6f58c08c0fe6274cdc31e3760e54fd86fba87b217793fabe4`。

静态核对：四份订阅每份只修改湖南卫视的一条媒体URL；候选清单仅更新正式哈希绑定。自动检查结果与最终发布提交以本轮 PR 的 Checks 和合并记录为准。

刷新 APTV 后确认台标和节目确为湖南卫视，再观察是否仍有转圈、停顿、黑屏或声音异常。实际试播结果待用户反馈后记录；历史通过不代表当前已验收。
