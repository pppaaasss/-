# IPTV 频道换源交接（2026-10-03）

后续更新（10月3日22:51）：用户确认“cctv1和湖南卫视已通过本地测试”，两个现用地址已补录 good 反馈。CCTV-1 换源通过 PR #131 合并发布。CCTV-2 暂无可替换的合格备用。发布提交、检测依据及回退处理见 [CCTV-1、CCTV-2 交接](cctv1-2-backup-review-2026-10-03.md)。

## 接手时先看

最新交接时间：2026-10-03 22:51（北京时间）。本表汇总 CCTV-1、CCTV-2、湖南卫视、CCTV-8 和 CCTV-11；湖南卫视换源经过及发布哈希保留为19:08时的历史记录。

| 频道 | 当前正式地址 | 当前状态 | 后续处理 |
|---|---|---|---|
| CCTV-1 | `http://219.147.245.238:25480/newlive/live/hls/1/live.m3u8` | 用户于10月3日22:51确认本地测试通过 | 当前地址已有 good 反馈；后续故障按最新交接处理 |
| CCTV-2 | `http://118.122.144.115:8888/newlive/live/hls/2/live.m3u8` | 现用地址保留；另一条合格备用缺口存在 | 读取最新家庭结果，寻找另一条未被否决的地址 |
| 湖南卫视 | `http://192.151.150.154/live/hnwshd.m3u8` | 用户于10月3日22:51确认本地测试通过 | 当前地址已有 good 反馈；后续故障按精确URL记录 |
| CCTV-8 | `http://58.210.139.130:9901/tsfile/live/0008_1.m3u8?key=txiptv&playlive=1&authid=0` | 用户于10月2日23:50决定保留 | 以当前地址为基线，有新故障反馈或确认故障再处理 |
| CCTV-11 | `http://120.40.39.246:352/newlive/live/hls/12/live.m3u8` | 用户于10月2日23:50决定保留 | 以当前地址为基线，有新故障反馈或确认故障再处理 |

当前正式清单 SHA256（PR #131 发布）：`baa10b0cc16eec05ad397f53a9ccbe48c9afd29b4744ed7bf48d0730e07b2db2`。下文湖南卫视发布时的哈希属于历史记录。

CCTV-8、CCTV-11 的完整检测依据和保留决定见 [10月2日记录](cctv8-11-manual-trial-2026-10-02.md)。22:49用户要求文档交接；22:51用户另行确认 CCTV-1、湖南卫视通过本地测试，两个频道的待反馈事项已完成。

## 仓库与发布入口

- 仓库：[pppaaasss/-](https://github.com/pppaaasss/-)；正式订阅在 `master`，家庭状态在 `home-control`，家庭报告在 `home-reports`。
- 用户日常入口：[tv.m3u](https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u)。
- 同步发布文件：`tv-core.m3u`、`tv-easy.m3u`、`tv.m3u`、`tv-all.m3u`。
- 本轮发布后条目数分别为53、176、425、425；正式受管频道为53个。
- 状态入口：[status.json](https://github.com/pppaaasss/-/blob/home-control/status.json)。下一次判断必须读取当时的状态和报告；本交接没有证明整个测试队列已完成。

## 湖南卫视换源经过

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
- 截至本轮授权已经超过36小时有效期；换源时属于用户授权的历史源人工试播，没有新的媒体探测或资格延长；22:51用户确认本地测试通过后，已另行新增当前地址的 good 播放反馈。
- 原主地址的“偶尔卡顿”反馈写入 `config/home-route-feedback.json`，仅归属用户实际观看的精确地址；不将该症状套用到此前精简版芒果地址。

## 发布与验证

四份订阅 `tv-core.m3u`、`tv-easy.m3u`、`tv.m3u`、`tv-all.m3u` 同步换源。正式受管频道保持53个，`harvest/home-candidates.json` 的正式清单 SHA256 更新为 `f2defc19412d92d6f58c08c0fe6274cdc31e3760e54fd86fba87b217793fabe4`。

- [PR #129](https://github.com/pppaaasss/-/pull/129) 已合并，正式发布提交：`3f728f6acb3a9e4a1fcfa3d0d25facc21f6312df`。
- 换源提交：`f9b3ccf8d7c294bc67bdabea4685ba17f15b9991`。
- [home-regression](https://github.com/pppaaasss/-/actions/runs/37118532973/job/111189929908) 与 [inspect-backups](https://github.com/pppaaasss/-/actions/runs/37118532926/job/111189929756) 均通过。
- 静态核对：四份订阅每份只修改湖南卫视的一条媒体URL；候选清单仅更新正式哈希绑定。合并后已重新读取四份 `master` 订阅，确认新地址一致。
- 本轮通过 GitHub 发布订阅和文档，未进行路由器升级或额外直播探测。自动检查通过只证明相关回归检查通过，不能代替电视实际播放反馈。

## 本地测试确认

2026-10-03 22:51:33（北京时间，UTC `2026-10-03T14:51:33Z`），用户原话：“cctv1和湖南卫视已通过本地测试”。两个现用地址均在 `config/home-route-feedback.json` 的 good 中新增 `home_user_confirmed_local_test_passed` 记录。测试时长、设备及独立画质指标未提供；本次记录以用户确认的本地测试通过为准，历史自动检测时间和资格不刷新。

## 后续接手顺序

1. CCTV-1、湖南卫视当前地址已有本地测试通过记录；后续如有卡顿、黑屏或串台，记录症状和时间，再读取最新家庭证据寻找下一条不同地址。
2. 湖南卫视原主地址已记录偶尔卡顿；精简版原芒果地址没有获得同一故障反馈。不能把旧主地址重新标为健康，也不能借用其他频道检测结果。
3. 历史源继续保留原资格时间；只有新的真实家庭检测可以产生新的检测资格。湖南卫视、CCTV-11 的历史通过时间不能因文档更新而刷新。
4. 若需要再次换源，核对用户反馈否决和主机级否决，同步四份订阅，并重新计算 `tv-core.m3u` SHA256 更新 `harvest/home-candidates.json` 的绑定。
5. 发布前核对只修改目标频道、各频道无重复条目；完成相应检查并合并后，读取正式订阅确认地址已发布，再通知用户刷新 APTV。
6. CCTV-8、CCTV-11 已有保留决定，维护时以当前地址和相关文档为准；已被确认串台、黑屏的旧 CCTV-8 地址不得回流。

## 关联记录

- [CCTV-1、CCTV-2 核查、发布与回退交接](cctv1-2-backup-review-2026-10-03.md)
- [CCTV-8、CCTV-11 换源与保留](cctv8-11-manual-trial-2026-10-02.md)
- [10月1日 CCTV 维护交接及失败地址](cctv-maintenance-handoff-2026-10-01.md)
- 用户反馈：`config/home-route-feedback.json`
- 正式清单绑定：`harvest/home-candidates.json`
