# 傻子电视台 · APTV 订阅

> 2026-09-05 已获用户确认，通过 [PR #5](https://github.com/pppaaasss/-/pull/5) 合入 `master`（`ae0c5fdd59b20c41e40cb61177bb15d364f58d36`）。当前断点：等待用户把 U 盘插入 AC86U，下一步仅只读确认磁盘、挂载及路由器环境。发布器仍关闭、编号为空，尚未安装探针或进行真实影子验收。下方“待合并”表述若属于历史记录，不再作为当前待办。

给 APTV（iPhone、iPad、Apple TV、Mac）使用的家庭优先电视直播清单。

> 新自动维护链路已合入 master，处于待现场安装/影子阶段。当前四份正式清单未被迁移代码修改；等用户出院回家、U 盘装好并完成 AC86U 实际路径核验后才启用生产发布。

## 订阅地址

- 无脑稳定版（日常首选）：`https://raw.githubusercontent.com/pppaaasss/-/master/tv-easy.m3u`
- 高清地区版：`https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u`
- 完整备用版：`https://raw.githubusercontent.com/pppaaasss/-/master/tv-all.m3u`
- 央视和省级卫视正式基线：`https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u`

只想打开就看，在 APTV 里订阅第一条 `tv-easy.m3u`。

## 家庭优先自动维护

- GitHub 每天北京时间 `00:30` 只搜集公开清单文本，生成当天新增或变更的央视/省卫候选；云端不测试线路，也不能把候选直接升为正式源。
- AC86U 每天 `02:00` 从与 Apple TV 等价的家庭 DNS、出口、分流和 IPv4/IPv6 路径检查全部正式央视与省卫，并逐项验证当天新候选和即将过期的家庭备用。
- AC86U 每天 `13:00` 只检查全部正式央视与省卫；需要换源时直接引用凌晨已验证、仍在有效期内的家庭备用，下午不测试备用。
- GitHub 在 `02:25` 和 `13:25` 读取家庭报告。只有当前线路在同一轮家庭路径连续失败、备用具有合格家庭证据（凌晨本轮验证；下午引用有效的凌晨验证记录），且报告与当前正式清单完全绑定时，才按频道和旧 URL 精确替换。
- 好线路保持原样；`UNKNOWN` 永不替换；没有三天轮换，也没有单轮最多替换几台的限制。电视上确认的卡顿、模糊和串台拥有最高否决权。

四份清单在一个 Git 快照中发布。其他清单里本来健康的不同备用 URL 不会因为某一条正式线路失败而被覆盖。报告过期、家庭断网、分流路径未确认、清单已变化、路由器资源不足或大面积同时异常时，全部保持上一版。

## 香港链路已退役

香港主机不再检测、不再中转家庭报告，也不参与故障切换。旧香港脚本和三天轮换实现暂时保留作回退依据，但配置已关闭、GitHub 定时入口已移除，旧 VPS 周期脚本取得新版本后会先停用自身定时器再退出。

GitHub 的旧每 6 小时采集也已停用，由每天一次的家庭候选增量工作流取代。

## AC86U 部署状态

路由器端使用 Asuswrt-Merlin + Entware，状态、队列和日志都放在 64 GB USB 2.0 的 `/opt`。探针单线程、低优先级、不做转码，超过资源或运行时间预算会保存队列后退出，不影响原有上网和电视订阅。

`config/home-publisher.json` 目前故意保持 `enabled: false`，路由器安装器也默认本地影子模式。2026-09-05 同步与修复已完成并合入 master，等待 U 盘现场部署；测试与台单差异见 [`docs/work-review-2026-09-05.md`](docs/work-review-2026-09-05.md)。完整步骤见 [`docs/ac86u-home-probe.md`](docs/ac86u-home-probe.md)，实施断点见 [`docs/home-first-roadmap.md`](docs/home-first-roadmap.md)。
