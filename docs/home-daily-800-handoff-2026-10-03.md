# 每日家庭候选测试交接（2026-10-03）

本文件是本项目后续维护的总交接入口。**今后每次修改前，先读取 master 上本文件的最新版本，再读相关人工频道记录与最新控制状态。** 历史文档保留当时证据；若旧文档仍写“待确认”“待合并”，以本文件较新的明确决定为准，不据旧快照恢复已撤销任务。

用户已撤销上千条临时全池测试，改为每天以实际更新的 GitHub 清单发现新候选，目标800条、硬上限800；不足如实报告。代码已合并，旧池已停止；每日新采集和家庭端到端验收尚未完成。800是目标和上限，不是每日保证。

## 已部署版本与线上快照

本次只读核验基于 master `8f987a5eabc84ae8a3a99937035c26a88621ef25`，控制分支 `e488e70f4dc899773cb3aeb957e24ca0c8928f92`。状态生成于 **2026-10-03 04:32:08 UTC / 北京时间12:32:08**；以下数字是该时刻的快照，不代表几天后的实时状态。

| 变更 | 已合并提交 | 作用 |
|---|---|---|
| [PR125](https://github.com/pppaaasss/-/pull/125) | `dbe3289e0f57575489d2eda64005cb3236c2368e` | 优先未测主机，批内分散主机，兼顾频道；失败惩罚有上限，不永久删除主机 |
| [PR126](https://github.com/pppaaasss/-/pull/126) | `c0b33cf596383ece4231a0bd382f8f837243b967` | 撤销旧临时池，收齐已有租约后停止，保留历史 |
| [PR127](https://github.com/pppaaasss/-/pull/127) | `8f987a5eabc84ae8a3a99937035c26a88621ef25` | 每日真实更新来源、最多800新候选、共享预算及独立发布保护 |

- 两个旧 campaign `fullpool-home-test-20261002-c1fda683`、其 `-r2` 后继均为 `STOPPED_BY_USER`，没有未结租约。结果分别224、480，合计704条；与停止时逐项比较完全一致。**无需恢复旧池，不重建旧 manifest，不清空旧结果来凑每日新数。**
- `config/home-test-campaign.json` 已删除；`stop_temporary_campaigns=true`、`daily_verified_intake=true`、`publication_hold=true` 均已部署。
- 最新[控制运行37096807006](https://github.com/pppaaasss/-/actions/runs/37096807006)成功，[发布步骤日志](https://github.com/pppaaasss/-/actions/runs/37096807006/job/111128288579)返回 `manual_publication_hold`、`replacement_count=0`。四份正式台单和反馈文件相对PR126合并版本无差异。**自动换源没有恢复。**
- 当前状态 `WAITING_WINDOW`、`issued_task=null`。当天共享候选账本已占480/800（旧临时转场），没有重置，按计数最多还可增加320；此时已过当天候选窗口，不能据剩余额度要求日间补测。状态里的下一窗口可能是13:00正式源复查，不是开放候选测试。
- 新 `daily_800`：已测0、合格0、可用未测0、候选批次0、shortfall800、transition_temporary_measured480。旧 `night_sweep` 的0/744、queue745、tested10949分别是遗留计划/队列/累计历史口径，不能解释为744条新鲜候选，也不能把480转场结果算作新采集的实测。
- 合并后仍未见新版采集首跑或新版 manifest；master 中 `harvest/home-discovery-manifest.json` 仍是旧结构。当前0不是“四个新来源实际只有0条”的结论，真实文件更新、过滤后数量尚待采集。`delivery-receipt.json` 的 `received_batches=[]`，且语义为 `cloud_queue_persisted_not_tested`，不构成新候选家庭已测回执。最近心跳时间也不等于已完成新流程。

代码回归证据：[PR127最终CI37086872914](https://github.com/pppaaasss/-/actions/runs/37086872914)成功；这不能代替真实采集、交付与家庭验收。

## 现用频道与发布保护

先阅读 [CCTV-8/11最新人工试播与保留记录](cctv8-11-manual-trial-2026-10-02.md)：两台按2026-10-02的最新人工决定保持现地址；CCTV-1/4/9/13/14/16此前也已获用户确认保留。这里记录维护决定，不补造连续观看时长、不新增good反馈，不把“用户保留”写成“当前自动技术资格合格”。尤其CCTV-11采用历史资格已过期的源人工试播，不能延长原资格时间。

当前精确地址以 master 的 `tv-core.m3u` 和上述人工记录为准；四份正式订阅须一致。旧 [2026-10-01维护记录](cctv-maintenance-handoff-2026-10-01.md)中的“待确认”是历史状态，不应覆盖这里的较新保留决定。原假台、黑屏、卡顿及主机否决继续以 `config/home-route-feedback.json` 执行。今后收到新故障，记录频道、精确URL、症状与时间，再决定是否复核或人工替换。不得根据单次码流成功认定节目身份正确。

删除 `config/home-test-campaign.json` 撤下旧输入，同时在 `config/home-publisher.json` 独立设置 `publication_hold=true`。该检查在发布报告处理前返回，即使 `--apply --inspect-shadow` 也不能恢复自动换源。不能仅用 `enabled=false` 代替（现有 inspect-shadow 路径仍会继续检查报告）。今后恢复自动发布必须单独审阅最新用户反馈与报告，显式解除 hold。

`stop_temporary_campaigns=true` 禁止重新登记/派发旧临时池。已有未到期租约原样保留，收回最后家庭结果后标记 `STOPPED_BY_USER`；所有旧 candidates/results、批次与原正常 tested/archive/queue 均保留。取消候选池意味着不再调度，并非抹掉历史。旧池未测条目不会直接移入每日队列；只有在当天合格上游中重新发现并通过全部检查，才可成为每日候选。

## 每日流程

- 现有采集工作流北京时间00:30计划运行，GitHub cron 有延迟，不能保证精确启动。每天必须检查上游文件，取消“队列够800就跳过”的旧规则。
- `config/home-daily-sources.json` 是受审阅入口，纳入以下四个新文件及原有GitHub清单。它们只是待核验来源，不宣称家庭可播，也不承诺更新频率：
  - [zilong7728/Collect-IPTV — best_sorted.m3u](https://raw.githubusercontent.com/zilong7728/Collect-IPTV/main/best_sorted.m3u)
  - [kakaxi-1/IPTV — ipv4.txt](https://raw.githubusercontent.com/kakaxi-1/IPTV/main/ipv4.txt)
  - [HuckOps/iptv — live_cn.m3u](https://raw.githubusercontent.com/HuckOps/iptv/main/live_cn.m3u)
  - [niuber/iptv-api — output/ipv4/result.m3u](https://raw.githubusercontent.com/niuber/iptv-api/master/output/ipv4/result.m3u)
- 对具体文件查询路径提交历史，固定提交读取新旧文件字节。要求最新文件提交在24小时内且清单字节和解析后的频道/URL/选项确实不同；README、仓库其它文件提交、mode-only/相同内容/仅注释时间戳提交不算更新。历史不足时保守拒绝，不猜刷新频率。近8小时实际更新且相邻路径提交间隔不超过8小时的优先（4–8小时级更新优先；不是上游刷新保证）。
- 仅下载 GitHub 清单文本，无候选媒体请求或 DNS 探测。去重精确 URL/身份，排除正式源、普通/临时已测历史、archive、跨频道冲突、用户否决、非公网文字地址、请求选项、鉴权/签名/有效期参数等。`$` 备注未新增兼容例外，真实 options 不能剥掉。换签 URL 不作为新路线补数。
- 主机轮转兼顾频道，最多输出800条。静态入选只是“待家庭测量”，不是有效播放或合格。当天 manifest 绑定日期、证据摘要和正式台单；过期/缺失/不匹配时拒绝新候选，保留普通队列历史。采集失败输出短缺，不回填未验证旧池。
- 仍只有02:00–11:00 Asia/Shanghai派发候选；13:00/20:00现用源复查保持。日计数硬上限800，临时转场当天既有候选计数不重置。现有活跃租约不会中断；部署前已预约的数目不可能被追溯撤销。
- 每批最多4条、240秒、64MiB；日上限8GiB/54000秒、发现6GiB/32400秒和原路由器资源保护不变。受共享额度限制时停止，不扩窗、不降低标准、不重置账本。

## 资格、错误与不足说明

`harvest/home-discovery-manifest.json` 记录文件证据、每源失败、静态排除及800短缺。`home-control/status.json` 的 `daily_800` 记录真实普通候选已测/合格数、短缺、入选余量、批次数、正式/备用复查批次、实际批次启动间隔与剩余窗口粗估容量。

源错误分为 GitHub/API/文本下载失败、文件过期、无文件历史、字节未变、清单不支持；静态排除沿用 history/conflict/veto/unsafe 分类。家庭 UNKNOWN 仍是未知，不等同永久坏源；不修改家庭测量或资格判断。REJECTED 不等于“假台”，必须参照现有测量字段和人工反馈。

800条至少需200个满批次。若控制器+家庭+上传平均每批150秒，光候选即约8小时20分，尚未计正式53台首轮、必要复查和备用复核；因此9小时窗口内不能承诺800必达。`observed_batch_spacing_seconds` 含真实调度/回传开销，`estimated_additional_window_capacity` 仅粗估上界，不承诺能达到；内存/网络保护、带宽、预算、空回执、工作流延迟都会降低实际值。无法估算时为null，不编造吞吐。

## 下一次实际验收（尚未完成的工作）

1. 在 [采集工作流](https://github.com/pppaaasss/-/actions/workflows/harvest-home-candidates.yml)等待计划首跑，或由有权限的用户在GitHub页面选择 **Run workflow**，分支master，只启动一次。计划为UTC16:30（北京时间次日00:30）；以本快照计，下一计划点是北京时间10月4日00:30。GitHub计划可能延迟或漏跑，应看实际run，不把计划时间写成已执行。
2. 检查源文件路径提交、新旧字节摘要、解析内容变化、失败原因、入选与短缺。工作流通过受保护PR合入两个harvest文件；仅看到采集run成功还不够，确认产物已进入最新master、日期和正式台单绑定正确。11:00以后手动采集会面向次日，不强行加入当天候选窗口。
3. 查看下一控制run读取的master版本与 `daily_intake`，确认接受当天清单；对照 `daily_800` 区分发现、静态入选、入队、预约、家庭实测、合格各层数字。首次真实候选数和来源淘汰原因目前待填，不能预报800。
4. 02–11窗内检查任务ID、租约、交付回执、`home-reports`对应报告和结算记录是否对应同批。云端持久化回执不等于家庭完成。检查批内主机分散、老结果未重测、UNKNOWN未被永久判坏、共享账本不超过800；窗口外无候选下发。跨日不继承前一日的已用计数，同日不得人为清零。
5. 复核旧704结果及两个STOPPED状态不变，发布仍为 `manual_publication_hold`、replacement_count0，四份正式台单和用户反馈未被采集改动。把首跑实际run、产物提交、数量、家庭证据和未完成项补回本文件，避免另起分散交接。

## 故障排查与安全回退

| 现象 | 先检查 | 处理边界 |
|---|---|---|
| 没有新的每日清单 | 采集是否实际运行、日志、自动PR是否已合入master、清单日期 | 未运行与真实过滤后不足分开报告；不拿旧池填数 |
| GitHub/API失败或来源不足 | 每源failure、文件路径提交时间与内容证据 | 如实记录失败和短缺；不猜存储地址，不剥掉鉴权、签名或options绕过过滤 |
| 有清单却未下发 | 当地日期、02–11窗口、绑定摘要、daily_intake、共享预算、现有租约 | 不扩窗、不重置账本、不重复派发；同日新清单可唤醒旧已完成计划，但仍受预算限制 |
| 有任务无结果 | batch_id、租约期限、交付回执、home-reports、心跳时间及控制run | 未知仍为UNKNOWN，保留证据；不以CI或云端请求代替家庭测量 |
| 没有换源 | publication_hold与发布日志 | 这是当前保护状态，不是应绕过的故障；恢复自动发布须另行明确审阅与授权 |
| 正式频道新故障 | 最新人工交接、精确URL反馈、家庭历史证据及资格时间 | 用户保留决定不等于永久健康；不要复活已否决旧地址 |

曾通过工作流手动接口查询/调度遇到 **GET Forbidden**，本次工具对workflow专用读取URL也返回“不支持的端点”。这不是运行成功证据；不要反复尝试被拒接口，不改触发器或借其它工作流绕过。可由用户使用GitHub网页Run workflow，或等待现有计划。若网页同样无权限，交由仓库授权人处理。

回退应先保留最新状态、日志和产物提交，仅在明确授权后做最小变更；本交接不是执行回退的指令。若每日采集异常，可先暂停相应采集工作流，保留正式台单、反馈、累计测试和未结租约，定位后再恢复。不整包回退到PR126以前，不恢复旧临时manifest，不关闭 `stop_temporary_campaigns`，不清空state，也不顺带解除 `publication_hold`。运行代码回退需要单独审阅状态兼容性和共享预算，不硬重置控制分支。

家庭端沿用native-v1及现有CPU、内存、流量和时间保护；不需要重新安装路由器程序。本轮及后续纯文档维护不跑媒体、DNS或家庭任务。家庭端才提供播放健康证据，云端只处理文本与已收到的报告。账号令牌、完整私有材料、私人对话不写入公开文档；订阅方案变化后的产品功能可用性尚未验证，本文件不作承诺。

## 文件、分支与证据入口

所有相对路径以下均指本仓库，`home-control`和`home-reports`是独立分支，并非master下的同名目录。

| 位置 | 用途 |
|---|---|
| master：本文件；[CCTV-8/11人工记录](cctv8-11-manual-trial-2026-10-02.md) | 当前维护决定与历史资格边界 |
| master：`tv-core.m3u`、`tv-easy.m3u`、`tv.m3u`、`tv-all.m3u` | 正式订阅，同步维护；受管频道53个 |
| master：`config/home-route-feedback.json` | 精确地址/主机用户否决和反馈，禁止丢失 |
| master：`config/home-thin.json`、`config/home-publisher.json` | 调度、预算、旧池停止和独立发布保护 |
| master：`config/home-daily-sources.json` | 受审阅文本来源入口 |
| master：`harvest/home-candidates.json`、`harvest/home-discovery-manifest.json` | 每日输入与来源/过滤证据；先核对生成时间及格式 |
| master：`scripts/daily_home_intake.py`、`scripts/home_test_campaign.py`、`scripts/home_thin_control.py` | 每日筛选、历史去重/排序、调度账本；不要重复实现 |
| master：`.github/workflows/harvest-home-candidates.yml`、`.github/workflows/publish-home-decisions.yml` | 采集及控制/发布工作流；文档更新不应触发媒体测试 |
| [home-control/state.json](https://github.com/pppaaasss/-/blob/home-control/state.json) | 持久队列、tested/archive、批次、日账本和临时历史 |
| [home-control/status.json](https://github.com/pppaaasss/-/blob/home-control/status.json) | 当前窗口、daily_800与旧口径；必须看generated_utc |
| [home-control/delivery-receipt.json](https://github.com/pppaaasss/-/blob/home-control/delivery-receipt.json) | 云端接收语义，不能代替家庭实测 |
| home-control：`tasks/home-ac86u-8f8908f0fba9.json`及`.native` | 家庭任务与native传输文本 |
| home-control：`temporary-tests/` | 两个已停止campaign隔离结果，不进生产 |
| [home-reports分支](https://github.com/pppaaasss/-/tree/home-reports)下`inbox/` | 家庭原始报告，按批次/时间交叉核验 |
| master：`home-publish/latest.json`及[发布工作流](https://github.com/pppaaasss/-/actions/workflows/publish-home-decisions.yml) | 发布收据可能是旧值；以最新run日志、配置和实际台单共同判断 |

## 可复制的接手说明

> 请维护仓库pppaaasss/-。每次修改前先读master最新docs/home-daily-800-handoff-2026-10-03.md及其中链接的人工频道记录，再只读核对最新master、home-control、home-reports和Actions。保留用户确认的CCTV-1/4/8/9/11/13/14/16，区分人工保留与当前技术资格；publication_hold仍开启，不擅自恢复自动换源。旧临时池已撤销，704结果应保留，不恢复或重测旧池。每日只用实际更新并通过过滤的GitHub清单，目标/上限800但不保证完成，候选仅北京时间02–11，共享预算不清零。先完成文档所列真实采集、交付及家庭验收，报告实际数量、证据和阻碍；不以旧0/744、CI成功或云端回执代替新流程验收。遇Forbidden不绕过，必要时请用户从GitHub页面Run workflow或等计划。修改范围和恢复发布须遵守用户当次授权，并把结果更新回同一交接文档。
