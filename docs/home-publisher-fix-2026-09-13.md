# 家庭报告发布工作流失败修复

检查基线：`bf7353dcec2dc0fe5d538bf0b39d960594d73f6a`，2026-09-13。

## 已核对的失败

9 月 11 日至 12 日的 12 次发布失败有三个原因：

| 运行 | 次数 | 日志中的失败原因 |
|---|---:|---|
| #40–44、#46–50 | 10 | `home report.generated_utc is stale` |
| #45 | 1 | 读取主分支规则时，匿名 GitHub API 返回 `403: rate limit exceeded` |
| #53 | 1 | `master moved during home publication; refusing a stale update` |

证据：[首次过期失败](https://github.com/pppaaasss/-/actions/runs/34582326545)、[API 限流](https://github.com/pppaaasss/-/actions/runs/34647450458)、[发布与采集冲突](https://github.com/pppaaasss/-/actions/runs/34711709160)。

过期失败对应的已发布报告为 `20260910T131012Z-peak-2000-9258e122b043f2ed.json`。发布器先检查 18 小时有效期，再检查是否已处理；因此同一报告即使已经发布，过期后也会让每次轮询失败。

#53 在候选工作流 #43 合并后继续使用旧主分支提交，触发了正确的防覆盖检查。根因是采集和发布使用了不同的并发组。

最近真实发布为 [PR #44](https://github.com/pppaaasss/-/pull/44)，已合并 13 个换源决定。随后 #55–57 成功运行，但返回 `duplicate`，没有再换源。这些成功状态不能代替电视端播放确认，也不能作为关联回归测试已通过的证据。

## 修复行为

- 先完整校验报告结构、设备身份、内容哈希，再检查发布回执。已处理的报告在过期后仍返回 `duplicate`。
- 未处理的过期报告返回 `stale_report`，不写回执、不改播放列表，也不退回使用更早的报告。过期的重复报告和未处理报告均输出证据时间、年龄及 `evidence_expired`，并在 Actions 摘要显示等待新报告。
- 新报告继续执行时间校验、缓存备用有效期、正式台单绑定、人工否决与原有家庭证据要求；非法报告仍失败。
- 采集和发布共用 `production-publish-lock`，使用 `queue: max` 保留待执行任务，且不取消正在执行的任务。取得执行机会后显式检出最新 `master`，避免使用排队时的旧提交。并发提交前后的防覆盖检查继续生效。
- 两个工作流使用现有 `github.token` 通过 `gh api` 读取规则，继续验证原有分支保护条件。

并发设置依据：[GitHub 并发与排队文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)。认证请求依据：[GitHub CLI API 文档](https://cli.github.com/manual/gh_api)。

此修复修改仓库发布流程。它不需要升级家庭路由器；1.05 下载余量仍由现有家庭配置管理。

## 验证记录

- 使用 #40 的真实报告及对应回执，以 `2026-09-11T09:04:33Z` 为时间重放：原脚本退出码 2，错误为报告过期；修复脚本退出码 0，状态 `duplicate`、`evidence_expired: true`，并写出等待摘要。报告、回执、四份台单及候选文件逐字节未变。
- 移除测试目录内的回执后，重放同一过期报告：返回 `stale_report`，不产生回执或台单改动。
- 本地全量回归 357 项：347 通过、10 个已退役接口测试跳过。此后增加 CLI 退出码及运行摘要测试，由提交后的回归一起验证。
- 两个工作流的 YAML、共享锁、最新主分支检出、认证配置与所有 shell 步骤语法检查通过。

新增回归覆盖：重复报告过期后的幂等性、未处理过期报告不写入、过期报告仍拒绝非法证据、文件哈希被改仍拒绝、18 小时边界允许、未来时间拒绝，以及 CLI 正常结束并显示等待摘要。
