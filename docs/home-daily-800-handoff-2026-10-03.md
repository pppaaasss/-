# 每日家庭候选测试交接（2026-10-03）

本文件是本项目后续维护的总交接入口。**今后每次修改前，先读取 master 上本文件的最新版本，再读相关人工频道记录与最新控制状态。** 历史文档保留当时证据；若旧文档仍写“待确认”“待合并”，以本文件较新的明确决定为准，不据旧快照恢复已撤销任务。

用户已撤销上千条临时全池测试，改为每天以实际更新的 GitHub 清单发现新候选，目标800条、硬上限800；不足如实报告。代码已合并，旧池已停止；北京时间2026-10-04首轮438条新候选已完成采集、家庭测量和结算闭环。技术合格不等于用户已确认节目身份或持续观看体验，也不表示已换源。800是目标和上限，不是每日保证。

## 已部署版本与线上快照

本次只读核验基于 master `02b6087fe3543e61dc37276385b097d0b5bbfa24`。首轮最终[固定控制状态](https://github.com/pppaaasss/-/blob/b969b0427d888028d8ff1ad966e58928fa5a4e31/status.json)生成于 **2026-10-04 02:17:12 UTC / 北京时间10:17:12**；[固定完整报告](https://github.com/pppaaasss/-/blob/96ef2a6d66fac246395f3faec454f99f730450b3/inbox/home-ac86u-8f8908f0fba9/20261003T182031Z-primary-0200-dc2d21fd2b2eac8d.json)提交于北京时间10:17:15。以下是首轮完成快照，后续维护仍须读最新分支。

| 变更 | 已合并提交 | 作用 |
|---|---|---|
| [PR125](https://github.com/pppaaasss/-/pull/125) | `dbe3289e0f57575489d2eda64005cb3236c2368e` | 优先未测主机，批内分散主机，兼顾频道；失败惩罚有上限，不永久删除主机 |
| [PR126](https://github.com/pppaaasss/-/pull/126) | `c0b33cf596383ece4231a0bd382f8f837243b967` | 撤销旧临时池，收齐已有租约后停止，保留历史 |
| [PR127](https://github.com/pppaaasss/-/pull/127) | `8f987a5eabc84ae8a3a99937035c26a88621ef25` | 每日真实更新来源、最多800新候选、共享预算及独立发布保护 |

- 两个旧 campaign `fullpool-home-test-20261002-c1fda683`、其 `-r2` 后继均为 `STOPPED_BY_USER`，没有未结租约。结果分别224、480，合计704条；与停止时逐项比较完全一致。**无需恢复旧池，不重建旧 manifest，不清空旧结果来凑每日新数。**
- `config/home-test-campaign.json` 已删除；`stop_temporary_campaigns=true`、`daily_verified_intake=true`、`publication_hold=true` 均已部署。
- 首次[采集run37147508499](https://github.com/pppaaasss/-/actions/runs/37147508499)成功，经[PR138](https://github.com/pppaaasss/-/pull/138)合入当天清单：静态入选438条，供应短缺362条。该缺口是800目标与实际可用新候选的差额，不能用旧池或重复测试补齐。
- 最终 `SLOT_COMPLETE`、`report_complete=true`、`issued_task=null`；当天438条/110个候选批次全部结算，剩余未测0，日候选账本438/800，无在途候选。正式源首轮53/53、必要复测19/19、历史备用检查15条完成。
- 按当天manifest的438个唯一 `candidate_id` 与报告匹配，实测结果为 **QUALIFIED 25、REJECTED 266、UNKNOWN 147**，覆盖51个频道；技术合格候选覆盖22个频道。UNKNOWN仍是未知，不能写成147条确认坏源。
- 完整报告 `candidate_results` 共453条、合格27条，其中438条/合格25条属于当天新候选，另15条历史备用为合格2、拒绝5、未知8。不得把453或27当作每日新候选数。正式53台本轮为GOOD32、BAD12、UNKNOWN9；报告内7条REPLACE只是建议。
- 最终[控制run37170510634](https://github.com/pppaaasss/-/actions/runs/37170510634)成功，[发布日志](https://github.com/pppaaasss/-/actions/runs/37170510634/job/111342346086)仍返回 `manual_publication_hold`、`replacement_count=0`。**本轮未自动换源，自动发布未恢复。**

### 时间和统计口径

报告文件名及 `generated_utc` 沿用最早测量时间：UTC 2026-10-03 18:20:31，即北京时间10月4日02:20:31；不能据此判断完整报告只覆盖凌晨。`aggregation.newest_measurement_utc` 为UTC 2026-10-04 02:15:37，即北京时间10:15:37，结合10:17:12状态和10:17:15提交判断最终完成时间。

`candidate_sweep_complete=false`仍反映旧普通队列口径，不代表当天438条未完成；应联合当天manifest ID集合、`daily_800`、`night_sweep`和结算批次核对。旧0/744、旧queue、累计tested以及10月3日480条临时转场均不是今天新增实测。最终 `night_sweep` 为438/438、remaining0；供应短缺362并不表示还有362条任务待跑。完成后的剩余窗口容量估计也不是需要补测的任务数。

### 新技术候选与人工确认边界

25条新合格候选分布：CCTV-1/2/6各2条；CCTV-3/4/5/7/8/12/13/14/17各1条；北京、东南、广东、河北、黑龙江、江西、辽宁、陕西、深圳、浙江卫视各1条。以上只是该轮技术资格快照，不能保证未来有效。

CCTV-2和黑龙江卫视已有新的 `switch_reverified` 技术候选，但尚无用户节目身份和持续观看确认，本轮未换源。本轮现用CCTV-1、湖南卫视、CCTV-11为GOOD；CCTV-8因质量元数据不可用为UNKNOWN，河南卫视传输为UNKNOWN。用户保留决定不被单次技术结果自动改写，河南仍按最新人工决定暂停处理。

### 503中断及自然恢复

[run37156226202](https://github.com/pppaaasss/-/actions/runs/37156226202)在规则API GET返回HTTP503后失败，下游收取和调度均跳过；此时已测244条，4条任务 `3b7b112e…` 在途，随后租约于UTC22:18:50过期。该批实际上已在UTC21:49:01–21:49:25完成原测量，21:50:01上传临时文件，只是未进入报告分支，不能因缺少observations文件就断定家庭没测。

后由现有 **schedule** [run37163772615](https://github.com/pppaaasss/-/actions/runs/37163772615)于北京时间10月4日08:04自然恢复；[日志](https://github.com/pppaaasss/-/actions/runs/37163772615/job/111322414103)显示收取1个临时文件并写入该批观察记录。按租约内原测量时间晚收取，四个任务ID在批次历史中各出现一次，未重测。无需代码修复，没有绕过Forbidden、改触发器、空提交或人工重置账本。本次说明现有恢复路径有效，但计划延迟期间仍会停滞，不能承诺15分钟内恢复。

代码回归证据：[PR127最终CI37086872914](https://github.com/pppaaasss/-/actions/runs/37086872914)成功；上述固定家庭报告才是本轮实测闭环证据。

## 现用频道与发布保护

先阅读 [CCTV-8/11最新人工试播与保留记录](cctv8-11-manual-trial-2026-10-02.md)：两台按2026-10-02的最新人工决定保持现地址；CCTV-1/4/9/13/14/16此前也已获用户确认保留。这里记录维护决定，不补造连续观看时长、不新增good反馈，不把“用户保留”写成“当前自动技术资格合格”。尤其CCTV-11采用历史资格已过期的源人工试播，不能延长原资格时间。

CCTV-1与湖南卫视已于10月3日22:51获用户本地测试通过确认，现用精确地址及good反馈以[最新湖南/CCTV汇总](hunan-manual-trial-2026-10-03.md)和[CCTV-1/2记录](cctv1-2-backup-review-2026-10-03.md)为准，不沿用更早的CCTV-1地址。

另须先读[河南卫视最新人工暂停记录](henan-manual-trial-2026-10-03.md)：用户试播失败后要求暂停，仍未解决，不再拿同一线路旧签名作已验证修复。该反馈目前在文档中，不能只看机器反馈配置。[山西](shanxi-manual-trial-2026-10-03.md)、[广东](guangdong-manual-trial-2026-10-03.md)卫视此前人工发布不等于本地验收完成；后续以各自最新人工记录和反馈为准。

当前精确地址以 master 的 `tv-core.m3u` 和上述人工记录为准；四份正式订阅须一致。旧 [2026-10-01维护记录](cctv-maintenance-handoff-2026-10-01.md)中的“待确认”是历史状态，不应覆盖这里的较新保留决定。原假台、黑屏、卡顿及主机否决继续以 `config/home-route-feedback.json` 执行。今后收到新故障，记录频道、精确URL、症状与时间，再决定是否复核或人工替换。不得根据单次码流成功认定节目身份正确。

删除 `config/home-test-campaign.json` 撤下旧输入，同时在 `config/home-publisher.json` 独立设置 `publication_hold=true`。该检查在发布报告处理前返回，即使 `--apply --inspect-shadow` 也不能恢复自动换源。不能仅用 `enabled=false` 代替（现有 inspect-shadow 路径仍会继续检查报告）。今后恢复自动发布必须单独审阅最新用户反馈与报告，显式解除 hold。

`stop_temporary_campaigns=true` 禁止重新登记/派发旧临时池。已有未到期租约原样保留，收回最后家庭结果后标记 `STOPPED_BY_USER`；所有旧 candidates/results、批次与原正常 tested/archive/queue 均保留。取消候选池意味着不再调度，并非抹掉历史。旧池未测条目不会直接移入每日队列；只有在当天合格上游中重新发现并通过全部检查，才可成为每日候选。

## 不足800时扩源修补（待部署，首次线上效果待验证）

首轮438条是固定42文件的筛选结果，不是全GitHub供给枯竭。旧实现没有缺额搜索，这一遗漏不能归因于家庭网络或503。首轮15个文件通过真实更新核验，25个超过24小时未更新，2个HTTP失败；解析12543行规范化为6329行。其后排除范围外3416、当前地址36，同身份合并153，去除跨频道冲突7、不安全地址/请求选项792、历史1474、范围/人工否决13，剩438。153是旧报告差额与代码路径推得；新版明确记录identity_merges和规范化合并/无效总数。

本节描述草稿修补，不能当作已部署或已发现新增真实源。固定注册文件不足800时，搜索近期有提交的IPTV项目（一次查询最多20个），读取目录中的清单文本；仓库活跃仅作线索，仍逐文件验证24小时内新旧提交、字节和解析内容变化。只构造GitHub API/raw地址，不使用返回的下载URL，不执行上游代码；拒绝重定向、符号链接和危险路径。新候选仍复用全部历史、安全、否决和频道冲突检查，达到800立即停止。

资源范围不扩大：注册与搜索发现合计最多48个文件；所有搜索、目录、提交历史、新旧文本读取共用144次请求上限（原48文件每个最多3次读取的静态上界）。每响应12MiB、单次20秒保持；新增20分钟请求准入截止，整体工作流仍30分钟，截止前开始的一次读取最多再用20秒。原42注册文件已占42个名额，因此当日扩源最多再尝试6个文件，请求额度或时间也可能先耗尽。48个名额全占时如实报告文件额度耗尽，不偷偷扩大范围。

采集报告新增expansion，记录查询、检查项目数、树截断/搜索不完整、注册入选数、扩源净增、请求/文件消耗以及搜索失败或有限搜索耗尽原因。有限搜索结束不代表全GitHub无源；仍不足时保留准确缺额。注册来源和搜索来源均保留文件级证据与origin；未修改固定注册表、凭据、权限、家庭预算、02–11窗口、hold或频道。本日窗口已结束，不追加实测。

部署后首次验收：确认expansion实际产生查询及新项目文件证据，检查每次淘汰/限额原因和真实净增；再看次日manifest及家庭结果。确定性测试只验证代码行为，不能写成已找到362条或保证每天测满800。

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

## 下一位接手者应做什么

首轮采集、交付、家庭测量和结算已闭环，**不要再以“首跑未完成”为由触发重跑**。当前没有需要追回的首轮在途任务，也不应恢复旧临时池。

1. 下次维护先读取最新master、本文件、人工频道记录及最新控制状态。新的故障反馈优先核对精确频道/URL、时间和症状；河南继续暂停，现用用户保留频道不擅自替换。
2. 观察下一次既定00:30采集及02–11家庭窗口。确认采集实际run、产物PR已合入master、当天日期与正式台单绑定正确；11:00以后采集面向次日。每次如实记录来源证据、过滤后入选数和供应短缺，不承诺800。
3. 用当天manifest的唯一ID匹配报告，分开统计新候选和历史备用，并核对候选批次全部结算、无在途。报告文件名可能沿用周期最早时间，应看最新测量时间、固定提交和状态生成时间；不要用旧普通队列complete字段覆盖每日闭环结论。
4. 再遇停滞，先查Actions失败步骤、临时上传区、观察文件与同一batch_id。已有租约内结果先收取，勿直接补测；API瞬时故障可由现有计划恢复，但不保证时限。被拒的手动接口不得换凭据、触发方式或浏览器代办绕过；必要时由用户本人在GitHub页面处理原失败运行。
5. 保持 `publication_hold=true`、旧704结果STOPPED及现有预算/窗口/资格门槛。对新技术候选的节目身份、持续观看体验以及任何换源，仍须按用户当次授权单独处理。记录7条REPLACE建议不等于执行7次替换。
6. 把后续真实run、产物提交、实测数量、反馈及未解决项补回同一文档；不扩大媒体测试，不为填满800重置账本或降低筛选标准。

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

> 请维护仓库pppaaasss/-。每次修改前先读master最新docs/home-daily-800-handoff-2026-10-03.md及其中链接的人工频道记录，再只读核对最新master、home-control、home-reports和Actions。保留用户确认的CCTV-1/4/8/9/11/13/14/16，区分人工保留与当前技术资格；publication_hold仍开启，不擅自恢复自动换源。旧临时池已撤销，704结果应保留，不恢复或重测旧池。每日只用实际更新并通过过滤的GitHub清单，目标/上限800但不保证完成，候选仅北京时间02–11，共享预算不清零。首轮438条已全部结算（25合格/266拒绝/147未知），不要重跑首轮；按文档观察下一日真实采集和测量，报告实际数量、证据和阻碍；不以旧0/744、CI成功或云端回执代替新流程验收。遇Forbidden不绕过，必要时请用户从GitHub页面Run workflow或等计划。修改范围和恢复发布须遵守用户当次授权，并把结果更新回同一交接文档。
