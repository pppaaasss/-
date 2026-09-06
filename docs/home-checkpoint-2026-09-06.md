# 2026-09-06 家庭传输检测与接续

## 09:44 手动扫描资源判断修正

- 09:40 批量进程 27860 在首次资源检查就 STOPPED，原因 `load1_above_1.5`；未开始频道探测，不能当作频道失败。结果为 `manual/tv-20260906-094045-27860.json`。
- 09:44 用户截图：load1/5/15 为 2.27/2.44/2.43，MemAvailable=119656 KiB（约 117 MiB），一次 top 显示 100% idle。单次 top 不证明持续空闲，但只凭 load1>1.5 就拒绝手动诊断过于粗糙。
- `manual_tv_scan.py` 改为启动前和每条线路前采样一秒 `/proc/stat` 差值；CPU busy>=85%、iowait>=20% 或可用内存不足 64 MiB（配置更高则遵守更高值）时停止。load1 仅记录。采样失败、无计时增量、非 iowait 计数回退或内存未知时停止；iowait 回退按零增量处理并明确记录，不把 guest 重复计入。阈值是手动诊断的运行选择，未修改正式 home_probe 的保护或路由验收条件。
- 每次打印 RESOURCES，JSON 与最终 SUMMARY 保存 last_resources。保留 nice=15、单条依次检测、20 分钟上限、独立锁、已测结果保留和精确临时规则清理。当前只需要从手机更新一个手动扫描脚本；已有临时目录的三份传输文件继续使用。
- 14 项手动扫描/资源测试与 18 项共享传输测试全部通过（32 项）；涵盖高 load 但 CPU 空闲、CPU/IO/内存压力、计数异常、guest 排除、启动拒绝、中途停止保存与清理。diff 检查通过。尚未在用户路由器运行新版，不能宣称 54 条线路测试完成。

计数说明依据 [Linux /proc 文档](https://docs.kernel.org/filesystems/proc.html)：iowait 并非精确的等待测量，计数可能下降；这里仅作为压力指示。下一步用户在 Termux 更新并启动，读取 `/tmp/iptv-manual-scan.log`。

## 09:28 最新现场结果与批量手动诊断

- 09:18：53 个提供者中读到 52 份文件，共 10,572 条规则；来源条件为空、未解析/嵌套为 0。类型计数：DOMAIN 245、DOMAIN-KEYWORD 112、DOMAIN-REGEX 1、DOMAIN-SUFFIX 9858、IP-CIDR 329、IP-CIDR6 26、USER-AGENT 1。
- 09:20：UA 位于 `rule_configs/Providers/Other/DAZN.yaml`，条件为 `USER-AGENT,DAZN*`。未读到的 ipcidr 提供者位于 `yaml_bak/zhuli500m/cb4394f308aeae2eb81b8f96633bcc8b`，实际错误 UnicodeDecodeError，文件存在；内容仍未解码核验。
- 09:25：直接向 192.168.50.1:53 发 TCP DNS 请求，GitHub 的 A 返回 RCODE=0/ANSWER_RECORDS=4；AAAA 返回 RCODE=0/ANSWER_RECORDS=0，均未截断。这是正常空 AAAA 答复，不是 DNS 超时，不证明全家 IPv6 路径是否可用。
- 路由器直接 curl 再次遇到 28/56 超时；改为 Termux 下载三个文件，再通过 tar + SSH 一次传到 `/tmp/iptv-transport-check` 并执行。**手机传文件成功**，以后优先使用此方式，避免重复从路由器直接下载。手机当前回到 `~ $`，不是路由器 SSH 提示符。
- 09:28 实测修正版单频道检查：CCTV-1 GOOD、两份样本、1080p/H.264、较低下载速度 29.1 Mbps；IPv4=7/IPv6=0；DNS 总计 A 有答复 1、AAAA 空答复 1，错误均为 0；TEMP_RULE REMOVED。脚本仍显示 NOT_AUDITED 是因为它只计引用，独立文件检查证据见上述记录。

新增独立 `manual_tv_scan.py`，下一步由用户从手机传入临时目录后后台运行：

- 以 tv-core 的频道键确定受管范围，但**每条检测 URL 来自电视实际 tv.m3u**。准确核对后是 53 个频道、54 条不同地址：江苏卫视普通版与 core 相同，另有江苏卫视4K 条目。此前“江苏卫视地址不一致”的表述遗漏了这个变体，现予纠正；普通 53 台与 core 没有该地址冲突。两条江苏线路都单独测试，不任意选一条。
- 每台两份 2 MiB 上限样本并请求 ffprobe 元数据；质量未确认不显示 GOOD。缺失频道不自动采用 core 地址；同一频道的不同 TV URL 全部单独测试，标称 4K 的条目最低高度为 2160。只做一轮诊断，不查备用、候选或生成替换决定。
- 每台之间检查资源，低优先级运行，20 分钟预算；时间到/中断保留已完成结果和未测频道，清理精确临时规则。同类手动扫描用独立 flock 防重入。
- JSON 单独保存到 `/opt/var/lib/iptv-home-probe/manual/tv-日期时间-PID.json`，schema 为 `iptv-manual-tv-scan-v1`，route_verified=false、production_use=false，不能作为正式家庭报告上传/验收。输出台名、URL 哈希和质量字段，不打印原始线路地址。
- Shell 后台日志为 `/tmp/iptv-manual-scan.log`；仅手动命令启动，未加入安装器/cron，原运行配置和四份台单不变。
- 七项新测试通过：真实电视 URL 选择、排除非受管台、多地址全测/缺失不回退 core、4K 变体质量要求、缺元数据 UNKNOWN、中断清理及保存、预算停止和核心范围校验。共享传输原有 18 项测试本轮也通过；Python 编译、diff 检查通过。批量家庭结果尚待用户运行，不能把这些测试当成家庭 53 台检测通过。

下一步获取批量启动/进度/最终 SUMMARY，再根据问题频道处理；家庭路径最终确认、ipcidr 文件解码、电视 IPv6 DNS 差异、GitHub 保护/配对、四份跨 18 小时影子报告与晚高峰策略仍未完成。批量诊断不会绕过这些门槛启用生产。

## 09:13 接续更新

用户截图 `1000021694.jpg` 显示当前配置 behavior 计数：52 classical、1 ipcidr。
新增独立 `router/ac86u/rules_check.py`，从正在运行的 Clash 进程读取 -f/-d，
只读检查 rule-providers 下的本地 yaml/text 文件。输出提供者/文件数量、读取缺失、
未解析或嵌套引用计数、规则类型与来源条件计数，不输出规则值或订阅凭据。
普通块状 YAML 以外的结构、二进制/MRS、缺失文件保持未检查；脚本不修改或重新加载
Clash，也不自动确认 route_context。磁盘文件检查不能替代运行中策略和家庭路径验收。
四项测试覆盖提供者区段隔离、嵌套来源条件、未知格式、相对/绝对路径与缺失文件；全部通过。
下一步由用户在路由器运行该只读脚本，回传 RULE_FILES 统计。此前 56 项测试和
下方 00:21 实测结果仍为各自阶段的证据，不把四项新测试算作新的家庭实测。

规则格式参考：[规则集合](https://wiki.metacubex.one/config/rule-providers/)、
[路由规则](https://wiki.metacubex.one/config/rules/)。实际固件能力以现场文件为准。

## 现场结论

用户在北京时间 00:21 提供 `1000021657.jpg`。执行的是功能提交
`0cb559263e4400f22c870d0389b8d1061d9f1f3b` 的三个临时文件，
位于 `/tmp/iptv-transport-check`；原探针安装目录没有被覆盖。

| 截图结果 | 可确认的范围 |
| --- | --- |
| `CLASH_RULES: source_sensitive=0 rule_set_refs=53` | 内联规则的启发式计数为 0；53 个外部引用的内容未审计，不能证明没有按来源分流 |
| `LAN_DNS: OK, A=4 AAAA=0` | 查询 LAN DNS 后获得四个 IPv4 地址；旧诊断未区分空 AAAA 与失败的 AAAA 查询 |
| `TV_PLAYLIST: 425 channels` | 成功下载电视实际订阅 `master/tv.m3u` |
| CCTV-1 `GOOD`, `sample_count=2` | 仅该频道本次两份样本通过，不能代表其他频道/备用或晚高峰表现 |
| `height=1080`, `codec=h264`, `deep_checked=true` | 本次元数据检查成功 |
| `min_download_mbps=32.968`, `error=""` | 两份样本中较低下载速度约 33 Mbps；这是下载吞吐，不是视频编码码率或全网带宽 |
| `CONNECTIONS: IPv4=7 IPv6=0` | 共享传输记录到七个成功 IPv4 连接；没有实际 IPv6 成功连接 |
| `TEMP_RULE: REMOVED` | 程序报告已移除本轮精确临时规则 |

这次家庭实测证明 LAN DNS、标记 IPv4 与 ffprobe 组合至少能检查这一条 CCTV-1。
它不是正式 run，不写家庭报告、不设置 route_context、不启用发布。
最后返回 Shell 提示符，诊断已经结束，并没有在后台继续扫描 425 台。

## 用户休息期间完成的工作

- 复核共享传输实现，修复“一个地址族查询失败时，把另一个地址族结果缓存为完整结果”的问题：仍允许使用成功地址族，但不缓存这种不完整结果，下次解析重新尝试失败地址族。
- 为 A/AAAA 分别增加有地址、正常空答复、错误的查询次数，不记录域名、频道 URL、节点或密钥。缓存命中不算新查询。
- 手动诊断现在会明确显示 `LAN_DNS: PARTIAL`、查询统计、`IPV6_PATH: NOT_TESTED` 和规则集内容未审计提示，防止一次 IPv4 成功被误解为双栈/全路径验收。
- 56 项相关测试通过，包括真实本地 TCP DNS、HTTP/HTTPS、ffprobe HLS 播放列表与分片、IPv6 成功及回退；新增三项覆盖空 AAAA、部分查询失败后恢复、全部 DNS 失败。防火墙操作在本地测试中模拟，测试输出的推送/连接成功不是新增家庭或线上报告。
- 复查远端 master 仍为 `17a8032a3a8f806b0ad94efdb96926bddd2cae6d`。对应四份台单未修改；本地核对实际电视清单 425 台、核心清单 53 台，受管频道只有江苏卫视地址不一致。
- 更新安装说明和交接顶部，移除“尚未插盘/安装”的过期当前状态。旧阶段记录保留为历史。

验证命令：

```sh
python -m unittest tests.test_home_transport tests.test_ac86u_home_probe tests.test_home_deployment_regressions tests.test_ac86u_installation tests.test_ac86u_route_check -q
```

本次不重跑全仓历史基线；此前基线中的 17 项问题另见旧测试记录。
本次修复只保存到 `home-first-ac86u`，未部署到路由器，也未合入 master。
这里没有到用户家庭 LAN 的 SSH 会话，无法替用户通宵执行家庭检测。

## 明天从这里继续

1. 保留已有 U 盘、Entware、安装目录和 probe_id。先核对当前 Clash 外部规则集，重点查来源 IP/设备分组；只读获取必要计数/规则类型，不发送整份订阅、密钥或节点配置。53 是引用次数，不保证是 53 个不同文件。
2. 用更新后的诊断区分正常空 AAAA 和 DNS 失败，并核对 Apple TV 显示的另一 IPv6 DNS。零 IPv6 连接不能证明 IPv6 路径已通过。
3. 解决实际订阅 tv.m3u 与核心报告绑定中江苏卫视 URL 不同的问题。不能用核心另一地址的 GOOD 代替电视实际线路，也不能未经对应家庭证据直接改正式台单。
4. 以上确认后，再准备持久化共享传输、验证重启/卸载清理和正常计划任务运行，随后进行家庭影子报告验收。保留原有多线路分流及选组，不固定代理节点。
5. GitHub 配对、受保护发布设置和至少四份不同时间、跨度 18 小时的有效家庭影子报告仍待完成；单频道手动诊断不能充当其中一份。

晚高峰“只查正式台、不换源不查备用；夜间确认差的源次日凌晨处理、白天恢复不自动洗白”的用户需求仍待实现，时间也尚未最终确定。不能因本次诊断成功而标记这些功能已完成。

用户最后要求先继续本地工作，次日再回复；今晚不需要用户继续输入命令。
