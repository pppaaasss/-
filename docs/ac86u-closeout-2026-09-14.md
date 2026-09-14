# AC86U 最小采样版现场交接（2026-09-14）

## 已有实机证据

- 路由器：AC86U、aarch64、时区 +0800，现有 VPN 使用 `merlinclash`。
- 手机安装版本：`ae703187e2219dcda08cbc4105c78acd3f3fa912`，运行目录 `/opt/share/iptv-home-native`。
- 路由器运行包四个文件，共 39,936 字节。Python 留在手机管理入口及 GitHub，路由器没有新增解释器、解码器或包管理器。
- 单批实测：北京时间 22:54 完成并上传；GitHub 在 22:55 接收 4 条测量。
- 进程组及监护进程峰值 RSS 为 11,404 KiB，最低可用内存 112,028 KiB，整个批次 34.073 秒；视频采样记录 26 秒、14,596,648 字节。这是这一批的实测，不代表所有频道的峰值。
- 云端入库运行：[34858695586](https://github.com/pppaaasss/-/actions/runs/34858695586)，成功。该批临时媒体资产已在持久入库后删除。
- CCTV-1 得到 1080p/h264/50 fps；CCTV-2、CCTV-3 的画质信息不足，归一化为 UNKNOWN；CCTV-4 为一次超时。不能把这四条都称为合格，也不能据一次超时直接换源。
- 正式历史和严格恢复的试验历史已迁入 GitHub，保留 7,347 个已测标识；不完整原件仍留在恢复备份中，未用猜测数据填补。
- 23:14 用户回执：`CLEANUP_OK freed_KiB=32948`，旧任务 0，VPN 规则未改动。完整旧文件备份在手机私有目录 `~/iptv-legacy-backup-*/legacy.tar`，同目录有 `SHA256SUMS`。
- 清理后用户已按手机入口执行 `enable` 并回复完成。启用后的第一轮持续运行及真实重启恢复仍需后续证据；云端派发任务本身不能证明家中开关开启。

## 运行与查看

北京时间 02:00–08:00、13:00–16:00、20:00–23:00 允许测速。定时入口每 5 分钟检查一次；有未过期任务且内存、CPU 满足条件才采样。GitHub 定时触发可能延迟，因此 02:00 是允许开始时间，不承诺整点收到第一个样本。

- 云端进度：[home-control/status.json](https://github.com/pppaaasss/-/blob/home-control/status.json)。`generated_utc` 是控制器更新时间，`last_heartbeat.measured_utc` 才是家庭测量时间。
- `progress.current_first_pass_completed/current_total` 表示该轮现用源首测覆盖数；复测、备用复核和新候选分别计数。
- `progress.belongs_to_active_window` 为 false 时，展示的是上一轮进度。窗口外保留上次数据，不伪装成当前已完成。
- `progress.report_complete` 只表示完整测量报告已生成。正式换源还需看 [home-publish/latest.json](https://github.com/pppaaasss/-/blob/master/home-publish/latest.json) 的时间、报告摘要及替换数量。
- `queue` 是候选队列数，`tested` 包括迁入的历史，二者都不是今天新测数量。
- `router_enabled: null` 表示云端无法直接读取家中的启用开关。`issued_task` 只表示派发；没有新家庭结果就不能声称路由器正在测速。
- `budget` 包含已使用额度和尚未结算的预留。它不是宽带账单，也不能拿来当作纯视频下载量。

候选搜集在 GitHub 每日北京时间 00:30 调度，只读取来源清单。家庭测量是画质和可用性的依据。保持 1.05 倍下载余量、两次明确失败才判 BAD、UNKNOWN 不换源，以及四份播放列表一起发布的规则。

## 异常处理顺序

1. 先读取云端状态、当前任务有效期和最新 Actions 失败步骤；修复云端问题后再考虑让用户执行手机命令。
2. 一个允许时段后仍没有新家庭时间，不能把旧心跳当成成功。先排查是否未派发、派发过期或云端任务失败。云端健康但家庭无结果时，才需要一次短的手机 `status` 输出。
3. 上传中断优先上传原断点；资源不够自动等后续检查，不能通过提高 AC86U 的资源上限来掩盖问题。
4. `PAUSED` 尤其 `ROUTE_CLEANUP_FAILED` 需要核对原因，不能盲目删除标记。数据目录及锁文件必须保留。
5. 完整报告未形成时，播放列表保持最近一次已验证的发布结果；应报告尚未完成，而不是声称已筛完。

## 重启和恢复边界

启用标记存于 `/opt/var/lib/iptv-home-native/ENABLED`；安装器在 `/jffs/scripts/services-start` 的 IPTV 块内注册 `IPTVHomeNative` 定时任务。旧文件清理已验证启动文件摘要未变化。这里证明配置保留，尚未做现场重启测试，不为验证而打断用户 VPN 或电视。

旧目录仅保留新版启动必需的 `thin-mode.locked`、`before-native-v1/staged`、小型迁移快照及原有锁文件。旧程序、历史、专用配置和密钥已经清理，**原 `rollback` 不能直接用于清理后的目录**；需要恢复时，先使用手机完整备份并审核旧版负荷。共享 Entware 和 VPN 依赖没有卸载。

GitHub 令牌按用户创建页于 2026-12-13 到期。到期前更新手机及路由器专用凭据，不将令牌写进仓库、聊天或诊断报告。
