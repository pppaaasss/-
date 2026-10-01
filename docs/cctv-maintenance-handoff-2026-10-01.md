# CCTV 维护交接（2026-10-01）

截至北京时间 21:20 的本轮维护记录。实际电视反馈优先；以下“现用”只说明订阅地址，不代表已经通过用户播放验收。

## 用户要求与现用地址

用户已授权先换历史资格过期的地址，由电视实际试播；随后要求继续替换黑屏的 CCTV-8。CCTV-1 当前播放流畅，用户明确要求保留。CCTV-13、14 原假台永久否决，有合格新源再替换。

| 频道 | 四份订阅现用地址 | 本轮实际反馈和待办 |
|---|---|---|
| CCTV-1 | `http://221.226.51.220:50081/newlive/live/hls/1/live.m3u8` | 用户确认不卡，保留；此地址原来错标 CCTV-16，实际为 CCTV-1 |
| CCTV-4 | `http://198.204.228.26/live/cctv4hd.m3u8` | 原带鉴权地址无法播放，已换历史源；新地址尚待用户确认 |
| CCTV-8 | `http://183.129.255.66:8480/hls/9/index.m3u8` | 两条备用失败后恢复原源；今日检测速度不足，等待实际试播 |
| CCTV-9 | `http://219.147.245.238:25480/newlive/live/hls/10/live.m3u8` | 原地址卡顿，已换历史源；新地址尚待用户确认 |
| CCTV-13 | `http://222.223.41.27:8888/hls/13/index.m3u8` | 原假台已拉黑，历史试播源尚待用户确认 |
| CCTV-14 | `http://219.147.245.238:25480/newlive/live/hls/15/live.m3u8` | 原假台已拉黑，历史试播源尚待用户确认 |
| CCTV-16 | `http://219.147.245.238:25480/newlive/live/hls/17/live.m3u8` | 恢复独立体育频道条目，尚待用户确认身份和稳定性 |

## 已确认失败的地址

本轮新增否决均写入 `config/home-route-feedback.json`；早期否决记录保留。以下地址禁止因为码流探测成功就重新当作合格备用。

| 频道 | 地址 | 用户反馈 |
|---|---|---|
| CCTV-1 | `http://183.129.255.66:8480/hls/1/index.m3u8` | 卡顿；后来换成上表地址并确认不卡 |
| CCTV-4 | `https://live-play-hls.cctvnews.cctv.com/CCTVChannel/channel_cctv4_mbd.m3u8?auth_key=1789894800-1-b59134a2ead43d7f783f943c8f535e7bb80c5169575b2e8efcd5b3ddea21b41c-6bab53f5027e03d79ba0938fa05c3082&yid=b59134a2ead43d7f783f943c8f535e7bb80c5169575b2e8efcd5b3ddea21b41c` | 无法播放 |
| CCTV-9 | `http://183.94.146.79:9901/tsfile/live/0009_1.m3u8?key=txiptv&playlive=1&authid=0` | 卡顿 |
| CCTV-13 | `https://event.pull.hebtv.com/jishi/cp1.m3u8` | 假台 |
| CCTV-14 | `https://event.pull.hebtv.com/jishi/cp2.m3u8` | 假台 |
| CCTV-8 第一次备用 | `http://118.122.144.115:8888/newlive/live/hls/9/live.m3u8` | 21:14 确认实际为 CCTV-9，记录 `home_wrong_channel_cctv9` |
| CCTV-8 第二次备用 | `http://101.66.198.201:9901/tsfile/live/0008_1.m3u8?key=txiptv&playlive=0&authid=0` | 21:20 确认黑屏，记录 `home_black_screen` |

CCTV-8 归档另有 `http://58.56.162.102:4466/newlive/live/hls/9/live.m3u8`，但该主机因其他频道反复家庭播放失败已被主机级否决，本次不重新使用。恢复的原 CCTV-8 地址没有用户串台或黑屏否决记录，但今日家庭检测下载 3.499 Mbps、流码率 7.979 Mbps，仍存在卡顿风险。

## 改动与提交

- [PR #114](https://github.com/pppaaasss/-/pull/114)：假台、无法播放等反馈入库；原错标 CCTV-16 地址移到 CCTV-1。修改 `scripts/home_thin_control.py`，使当前频道的精确 URL 被用户否决时无法因媒体探测成功仍显示健康；实际检测次数仍由真实报告决定。补充两项回归测试。
- [PR #115](https://github.com/pppaaasss/-/pull/115)：记录 CCTV-9 卡顿及 CCTV-1 当前流畅；添加只读工作流 `.github/workflows/inspect-home-backups.yml`，从 `home-control` 读取历史备用，不主动检测媒体、不改家庭状态或订阅。
- [PR #116](https://github.com/pppaaasss/-/pull/116)：按人工试播授权同步四份订阅，替换 CCTV-4、8、9、13、14，恢复 CCTV-16；添加历史源试播文档。
- [PR #117](https://github.com/pppaaasss/-/pull/117)：CCTV-8 第一条备用串台入黑名单，改用第二条历史备用。
- 本交接所在提交：CCTV-8 第二条备用黑屏入黑名单，四份订阅恢复原 CCTV-8 地址；更新试播记录、README 入口与本交接。

四份订阅为 `tv-core.m3u`、`tv-easy.m3u`、`tv.m3u`、`tv-all.m3u`。每次正式 URL 变更同步 `harvest/home-candidates.json` 的 `formal_playlist.sha256`，频道数保持 53。本次值为 `c1f2b993093bc0958a7de52c63293139a21a598355f7f220f78796b79500647d`。本次只改 CCTV-8 URL，其余现用条目沿用前次发布。

## 证据和验收边界

历史备用盘点基于 `home-control` 提交 `ccf63d672751605f0744661b839262e7ff8046ca`，最新家庭报告时间 2026-10-01T06:37:31Z。[只读盘点日志](https://github.com/pppaaasss/-/actions/runs/36866063797/job/110381735965)及[家庭反馈记录](viewer-feedback-2026-10-01.md)保留完整来源与旧记录。此库存不是未来实时库存。

PR #114 的相关本地测试 75 项通过；本轮此前 PR 的 GitHub 回归检查均通过，检查覆盖 506 项测试。本交接所在 PR 的最终检查和发布结果以该 PR 的 GitHub Checks 与合并记录为准。回归测试通过不等于电视播放成功；不能因此刷新历史资格时间或添加用户 good 反馈。本次维护通过 GitHub 发布订阅与云端代码，没有进行路由器升级或新的媒体探测。

## 接手后的处理

1. 用户刷新订阅，确认 CCTV-8 是否显示电视剧频道、能否播放和是否卡顿；CCTV-4、9、13、14、16 也需要逐台实际反馈。
2. 获得确认后只记录对应频道和精确地址的真实反馈；未确认的试播源继续标为待确认。
3. CCTV-8 两条已否决备用不再回退使用，主机级否决继续生效。原源也失败时记录故障，等待新的合格来源；不要把已失败地址循环替换。
4. 保留用户确认不卡的 CCTV-1 地址。假台 CCTV-13、14 旧地址不得回流；换源必须使用另一个真实频道地址。
5. 后续修改同时核对四份订阅、候选绑定和文档，不删历史备用证据，不虚构检测，不将其他频道播放证据借给本频道。

各次历史合格时间、试播地址与回退详情见 [历史源试播记录](cctv-history-trial-2026-10-01.md)。
