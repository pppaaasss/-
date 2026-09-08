# 电视项目稳定收尾记录 · 2026-09-08

状态：代码修复及本地验证已完成，用户已授权推送和合并，正在发布；路由器尚未部署。实际订阅已通过截图确认为 tv.m3u，尚未达到冻结条件。

基线：master `7d3214185ce6ed2b65f8b81ff21a290c63fe8b6f`，已包含 [CCTV5 修复 PR #26](https://github.com/pppaaasss/-/pull/26)。用户确认 CCTV5 串台解决，本轮不再换此台。独立分支：`fix/stable-closeout-20260908`。

## 已确认并修复

| 审计项 | 本次处理 | 本地证据 |
|---|---|---|
| 增量被覆盖 | 索引保存未确认批次；家庭持久化队列后写接收回执，专用回执上传不冒充健康报告；原 99 条旧清单可在第一次新收集时迁入待交付 | 漏接后重复交付、消失候选保留、收到确认才删除；队列写失败不确认；队列写后中断可补回执 |
| COMPLETE 后迟到 | 每次唤醒至多每 300 秒检查文本清单；有未测队列且无未完成主任务时建立补接任务；窗口外保存等待 | 04:00 补接，13:00 只排队；原永久 ID 去重测试通过 |
| 新反馈被旧缓存覆盖 | 每条 URL 绑定 bad/good 反馈状态，变化只使该 URL 缓存失效；备用及发布器继续核对否决 | 新 bad 不再产生 KEEP；无关 GOOD 仍复用 |
| UNKNOWN 丢历史 | 撤销本次有效资格，保留历史及失败结果，至少间隔 1 小时再按需确认；硬否决仍清除可用身份 | 原验证时间不变、有效池移除、历史仍在 |
| 异常丢已完成进度 | 单条地址 fsync 追加日志；原子快照后压实；恢复保留原检测时间、永久 ID 和未完成队列 | 候选第二条中断、正式源第二条中断、尾部半条写入、状态替换失败均有测试 |
| 部分报告误用 | 上传和发布均拒绝显式部分完成/候选阶段报告 | 部分报告不能进入上传队列 |
| 日志及历史 | 正式模式轮转、完成任务 14 天；已发布水位前的旧报告按 14 天保留规则清理；保留每时段首末及当前发布/待上传文件；回滚保留两版 | 清理不丢待办、永久 ID、待上传及首末报告 |
| 升级与恢复 | 全依赖固定版本包；已启用设备保留原激活；取消日期固定 ACK；持久事务标记先于停用；显式恢复及回滚 | 模拟非 Exception 的硬中断后恢复配置、代码，状态字节保持不变；重复恢复不启动测速 |
| 身份冲突 | 明确 name/key 不一致拒绝；同 URL 不同频道在采集、家庭候选/备用和发布时拦截 | CCTV5/5+、8/8K、4/4K 键区分；冲突候选不输出。没有加入 URL 猜台或 OCR |
| 文档和回归 | README 作为当前入口；六份旧记录加替代标记；新增 PR 回归工作流与精确覆盖审计命令 | 全仓回归和 Shell/YAML 检查 |

## 正式台单逐项处置

本轮没有修改四份正式台单，频道名、台标、分组、顺序及用户已修好的 CCTV5 均保持。没有把 core 健康证据套给另一条 URL。

用户在本轮后续消息中提供电视截图，明确实际使用 https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u，显示 425 个频道、41 个分组。根据文档 D1—D3，不擅自统一或删掉不同 URL 的可用线路。本次发布先保留各项差异，tv.m3u 的江苏卫视独立备用标为未验证的运行待办；后续基于精确家庭证据选择：同 URL 去重、补缺台，或将确需保留的独立备用纳入家庭检测；修改线路须有该地址的精确家庭证据。

| 文件 | 不同地址的频道 | 缺失 | 重复 | 处理状态 |
|---|---|---|---|---|
| tv-easy.m3u | 湖南卫视、河北卫视、北京卫视、山西卫视、山东卫视、新疆卫视、内蒙古卫视、宁夏卫视、广西卫视、天津卫视、西藏卫视、河南卫视、黑龙江卫视 | 东方卫视 | 深圳卫视 | 逐项保留；实际使用 tv.m3u，不同地址未验证 |
| tv.m3u | 江苏卫视 | 无 | 江苏卫视 | 逐项保留；实际使用 tv.m3u，不同地址未验证 |
| tv-all.m3u | 江苏卫视 | 无 | 江苏卫视 | 逐项保留；实际使用 tv.m3u，不同地址未验证 |

精确 URL、core 对应 URL 和重复条目见 [home-coverage-2026-09-08.json](home-coverage-2026-09-08.json)。审计命令：`python scripts/audit_home_coverage.py`。该工具只列覆盖，不能称为家庭实测。

## 已有运行证据与本轮限制

- 自动采集：[9 月 8 日 04:05 工作流](https://github.com/pppaaasss/-/actions/runs/34158054224) 成功，生成 99 条新增/变化候选。这批是否已经在家庭设备接收，当前仍无现场凭据。
- 家庭最新报告：北京时间 9 月 8 日 02:25:58，53 个受管频道，26 GOOD、26 BAD、1 UNKNOWN；25 个未解决。报告早于 04:05 清单，不能证明迟到清单也完成了。
- 旧版正式发布：[PR #23](https://github.com/pppaaasss/-/pull/23)、[PR #25](https://github.com/pppaaasss/-/pull/25)。`home-publish/latest.json` 仍记录 #25 的那次 2 路替换；不能拿其中 CCTV5 路由当成 #26 之后的现用路由。
- [9 月 8 日 09:27 发布检查](https://github.com/pppaaasss/-/actions/runs/34176686102) 成功，日志为 `status=duplicate`、`replacement_count=0`，未重复换源。
- 仓库 `config/home-publisher.json` 为 enabled=true，probe_id 为 home-ac86u-8f8908f0fba9。没有家庭执行通道，无法读取路由器当前完整配置、安装版本、真实日志或持久队列。不能声称路由器装好了本轮修复。
- 本轮所有新增行为测试是临时目录、模拟测量和本地 Git 报告分支测试；没有外部 IPTV 测速，也没有拿云端测试认定频道健康。

## 测试与旧失败项归因

首次 PR CI 暴露了测试环境差异：临时安装器测试读取真实 runner UID，普通 CI 用户被正确拒绝；已改为临时目录模拟 proc 身份，并同时验证普通身份必须拒绝、模拟管理员可安装。正式安装器权限规则未改。CI 同时安装 FFmpeg，以运行本地媒体夹具测试，避免缺依赖额外跳过。

基线全仓：326 项，306 通过、8 失败、12 报错。本轮全仓 343 项：333 通过，10 明确跳过，0 失败、0 报错。最新结果见 [测试证据](stable-test-evidence-2026-09-08.json)。10 项退役测试仍保留代码并明确跳过，不计入通过数。

| 原未通过项 | 归因与处理 |
|---|---|
| test_0200_refreshes_expiring_home_backups_even_when_github_candidates_are_disabled | 旧周期重扫预期违反现行规则；改为健康时不重扫、不刷新备用时间 |
| test_easy_gate_requires_three_checks_and_headroom | 旧云端大陆候选测速预期；保留三次检查，云端速度仅候选参考；家庭速度门槛未变 |
| test_core_route_requires_download_headroom_for_high_bitrate | 同上；只调整候选构建测试，不改变家庭 1.35 门槛 |
| test_visually_confirmed_shanxi_route_keeps_two_probe_fallback | 云端速度旧预期；仍验证二次检查和 recheck_failed 淘汰 |
| test_easy_list_keeps_usable_cctv_and_satellite_before_other_channels | 旧内部标记已不使用；验证实际入选频道和当前家庭核心候选条件 |
| test_existing_easy_core_is_carried_when_no_fast_replacement_exists | 不应无证据携带旧核心；改为未验证旧源不会盲目补回 |
| test_same_station_4k_variant_keeps_its_quality_floor | 用户已接受 CCTV4K 1080p，改正旧 2160 预期 |
| test_repository_config_excludes_core_playlist | 锁定频道从 7 增至 8 的旧夹具数字，按当前配置修正 |
| test_patch_replaces_old_visible_name_and_preserves_other_channel | 旧直写正式表流程退役；验证正式文件禁止修改，并在 preview 文件验证原编辑行为 |
| test_detached_upgrade_preserves_progress_and_enables_publishing | 旧日期 ACK 与时钟耦合；更新器不再重新激活，改为验证固定升级保留原激活和硬中断恢复 |
| test_repair_cctv5_pair 中 10 项旧 API 测试 | 实现早已改为只读候选检查，旧 CANDIDATES/Probe/choose_routes/rewrite_playlist 等 API 不存在；明确标为退役保留，新增现行只读接口与频道身份测试 |

10 项旧 API 测试名称：
- `test_station_keys_are_independent`
- `test_selection_uses_distinct_healthy_hosts`
- `test_selection_prefers_high_bitrate_with_download_headroom`
- `test_moderate_speed_with_headroom_beats_fast_soft_picture`
- `test_viewer_confirmed_1080_beats_fast_720_route`
- `test_new_builder_route_competes_with_fixed_rescue_pool`
- `test_pair_optimizer_does_not_waste_best_plus_route`
- `test_segment_duration_supports_real_stream_bitrate`
- `test_probe_uses_full_media_object_size_for_bitrate`
- `test_rewrite_removes_dead_pair_and_duplicate_backup`

## 部署、恢复、回滚

文档 A6 要求按本轮授权执行。用户随后明确回复“推送吧”，已授权这批远端推送和合并；PR 页面记录实际合并与 CI 状态。路由器需要用下方固定版本命令部署，尚未取得现场安装证据。

获准发布后，将固定版本 SHA 填入下面的一条 Termux 命令（不需要重复搬脚本）：

```sh
iptv_ref=发布后的40位提交SHA; curl -4 -fsSL --retry 3 "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/upgrade_from_termux.sh" -o "$TMPDIR/iptv-upgrade.sh" && sh "$TMPDIR/iptv-upgrade.sh" "$iptv_ref"
```

预期先显示“更新已交给路由器后台执行”，升级完成日志为 `BACKGROUND_READY`。更新在后台等待原批次结束，不扩大白天测速时段。查看 `/opt/var/lib/iptv-home-probe/background-upgrade.log` 及 `status.sh`；安装版本必须与目标 SHA 一致。

- 状态：`/bin/sh /opt/share/iptv-home-probe/status.sh`。
- 程序故障修复后显式恢复：`/opt/bin/python3 -E -s /opt/share/iptv-home-probe/runtime_status.py --recover`。保留首次故障诊断，验证本地文件及写入后由原调度继续。
- 升级中断：`/opt/bin/python3 -E -s /opt/share/iptv-home-probe/background_upgrade.py --recover`。
- 回滚：用 `installed-version.json` 中 rollback 目录名，执行 `background_upgrade.py --rollback before-background-...`。旧纯文本标记需明确对应旧备份目录，不自动猜选。
- 状态和媒体健康分开：接收回执只是“队列已保存”，未代表“已测完”；报告上传状态只是“GitHub 已收到”，未代表“正式发布”。

## 尚未满足的冻结条件

1. 远端发布已获授权，固定版本路由器安装尚未执行。
2. 实际订阅已确认为 tv.m3u；其江苏卫视独立备用没有精确家庭检测证据，暂时保留并明确列为未验证待办。easy 的差异、缺台及重复不是当前使用入口，保持逐项记录。
3. 本轮版本的凌晨、13:00、20:00 正常时段真实家庭回传未取得。
4. 本轮版本的迟到补接、中断续跑、反馈优先已通过本地行为测试，尚待现场证据。

因此不能宣布整个项目收尾完成或所有频道健康。剩余坏源不扩库凑通过，没有合格备用就保留待办。完成上述验收后停止主动优化，只修实际 bug。

## 旧报告中的未解决频道（仅历史运行待办）

这不是当前播放故障判定；用户最新确认优先。以下是 02:25:58 那份报告的原始未解决清单：

| 频道 | 当时原因 |
|---|---|
| cctv3 | no_home_qualified_backup |
| cctv5plus | no_home_qualified_backup |
| cctv10 | no_home_qualified_backup |
| cctv12 | no_home_qualified_backup |
| cctv17 | no_home_qualified_backup |
| cctv4k | no_home_qualified_backup |
| 江西卫视 | no_home_qualified_backup |
| 天津卫视 | no_home_qualified_backup |
| 西藏卫视 | no_home_qualified_backup |
| 黑龙江卫视 | no_home_qualified_backup |
| 河南卫视 | no_home_qualified_backup |
| 重庆卫视 | no_home_qualified_backup |
| 浙江卫视 | no_home_qualified_backup |
| 河北卫视 | no_home_qualified_backup |
| 海峡卫视 | insufficient_home_evidence |
| 兵团卫视 | no_home_qualified_backup |
| 安多卫视 | no_home_qualified_backup |
| 北京卫视 | no_home_qualified_backup |
| 吉林卫视 | no_home_qualified_backup |
| 云南卫视 | no_home_qualified_backup |
| 海南卫视 | no_home_qualified_backup |
| 宁夏卫视 | no_home_qualified_backup |
| 湖北卫视 | no_home_qualified_backup |
| 新疆卫视 | no_home_qualified_backup |
| 青海卫视 | no_home_qualified_backup |
