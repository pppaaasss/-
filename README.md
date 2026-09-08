# 傻子电视台 · APTV 订阅

当前状态入口，更新于 2026-09-08。GitHub 负责收集公开清单文本与发布家庭决定；AC86U 经与 Apple TV 等价的家庭路径检测。香港不再检测、中转或裁决。

发布器已启用，9 月 8 日已发生自动换源（PR #23、#25）。CCTV5 串台由 PR #26 修复，用户已确认解决。本次稳定收尾代码的部署、现场验收进度见 [收尾记录](docs/stable-closeout-2026-09-08.md)，不能把本地测试通过当成路由器已经升级。

## 订阅与检测覆盖

用户已于 2026-09-08 提供电视截图确认：实际使用 `tv.m3u`（截图显示 425 个频道、41 个分组）。后续覆盖收尾以此入口为准。

| 订阅 | 地址 | 受管频道检测覆盖 |
|---|---|---|
| 日常精简版 | [tv-easy.m3u](https://raw.githubusercontent.com/pppaaasss/-/master/tv-easy.m3u) | 13 个卫视地址不同于 core，另缺东方卫视、重复深圳卫视，待确认实际使用入口后处理 |
| 地区版 | [tv.m3u](https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u) | 主条目与 core 对齐，另有江苏卫视独立备用未纳入 core 检测 |
| 完整备用版 | [tv-all.m3u](https://raw.githubusercontent.com/pppaaasss/-/master/tv-all.m3u) | 与地区版相同的江苏卫视独立备用问题 |
| 央视与省卫基线 | [tv-core.m3u](https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u) | 家庭定时检测的 53 个受管频道 |

[逐地址差异清单](docs/home-coverage-2026-09-08.json)。同一频道的不同 URL 不共享健康证据；同一次发布四份文件，也不代表每条不同地址都测过。实际入口已确认是 tv.m3u；江苏卫视独立备用仍保留为未验证的待办，不擅自统一其他订阅的地址。

## 固定运行规则

- 北京时间 00:30：GitHub 收集文本。未收到家庭持久化接收确认的批次继续保留；只有家庭保存队列后才回传接收凭据。
- 02:00—08:00：检查正式源、必要备用和新增候选。08:00 不开始下一条，当前地址完成后保存，未完成跨日续跑。
- 13:00：只检测当前正式源，故障时使用仍有效的凌晨或晚高峰备用缓存；不测新候选或备用。
- 20:00：正式源晚高峰复核和必要的换前备用确认。
- 每 5 分钟唤醒调度及文本清单检查。已完成任务可以补接迟到候选，窗口外仅保存等待；不会每 5 分钟重扫媒体源。
- 健康源不换；UNKNOWN 不触发替换；用户 bad 反馈优先。没有三天轮换或单轮换台数上限。
- 已测候选永久去重；历史备用只在故障时按需确认。UNKNOWN 撤销本次资格、保留原证据和时间，至少间隔一小时再确认。明确否决不回流。
- 保留 50 MiB 暂停、58 MiB 恢复、H.264 3 Mbps、速度余量 1.35。CCTV-4K 接受 1080p，优先合格 2160p。

## 状态、恢复和升级

路由器终端查看：

```sh
/bin/sh /opt/share/iptv-home-probe/status.sh
```

查看版本、停机原因、任务、水位、待测和待上传数量、最近报告及未解决原因。最终发布以 [发布回执](home-publish/latest.json) 和最近 Actions 为准；上传成功不是发布成功。

程序故障排除后，显式恢复（保留首次诊断，不重装环境）：

```sh
/opt/bin/python3 -E -s /opt/share/iptv-home-probe/runtime_status.py --recover
```

已启用路由器用 [upgrade_from_termux.sh](router/ac86u/upgrade_from_termux.sh) 更新，参数必须是已发布的 40 位提交 SHA。手机一次下载全部依赖并交给路由器后台安装。升级等待当前批次结束，保留激活、配置、队列和永久去重；不依赖某日旧 ACK。后台完成标志为 `BACKGROUND_READY`。

升级中断恢复：

```sh
/opt/bin/python3 -E -s /opt/share/iptv-home-probe/background_upgrade.py --recover
```

主动回滚：对 `installed-version.json` 中的 `rollback` 目录名运行 `background_upgrade.py --rollback 目录名`。旧版纯文本 marker 需要明确指定对应的 `before-background-*` 目录，不自动猜选。若安装目录里的恢复脚本损坏，可使用 `/opt/tmp/iptv-stable-提交SHA/background_upgrade.py`。

检测按地址追加恢复记录；完整快照成功后才压实。部分完成的报告不能上传为完整换源报告。日志约 1 MiB 轮转，完成任务保留 14 天，升级回滚保留两版。待办和永久去重不清除；报告清理必须有发布水位，保留最近 14 天、待上传、当前发布证据及每个时段首末报告。

## 测试和停止条件

```sh
python -m unittest discover -s tests -v
python scripts/audit_home_coverage.py
```

PR 回归涵盖家庭调度、交付、反馈、去重、发布、恢复和台单覆盖。退役直写修台 API 的 10 项历史测试明确跳过，另有现行只读替代接口测试，跳过不计入通过数。

新版本部署后，须在正常时段收到凌晨、13:00、20:00 的真实家庭报告，并完成迟到补接、中断恢复及实际订阅覆盖确认，才能冻结。没有合格备用的频道保留待解决及原因。达到停止条件后，只处理实际 bug、播放反馈、外部协议失效和必要依赖兼容，不继续扩库或重构。

历史日期文档仅作过程记录；当前规则以本 README 和收尾记录为准。
