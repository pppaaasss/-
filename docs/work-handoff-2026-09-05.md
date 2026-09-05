# 2026-09-05 电视项目 Work 接班文档

> 2026-09-05 23:29 北京时间现场断点：U 盘、Entware 和探针已安装；仍未验证电视路径，尚无家庭报告。NETNS 和 owner 匹配现场测试失败。新增 `router/ac86u/route_check.py` 只供单次标记连接测试，尚未在路由器运行。发布器继续关闭。下方旧安装前状态属于历史记录，以本节为准。

这是额度中断后可直接续做的断点。继续现有实现，不从头设计；先读取本文件、[`home-first-roadmap.md`](home-first-roadmap.md)、[`ac86u-home-probe.md`](ac86u-home-probe.md) 和 [`work-review-2026-09-05.md`](work-review-2026-09-05.md)，随后获取实时 GitHub 分支。


## 23:29 现场进度（最新，后面的旧记录不覆盖本节）

- 路由器 RT-AC86U，386.14_2 Koolshare/MerlinClash，aarch64；用户通过手机 Termux SSH 操作。用户已授权初始化新 U 盘，不再重复询问格式化。
- `/dev/sda1` 已格式化 ext4、卷标 IPTV，挂载 `/tmp/mnt/IPTV`，容量 57.4 GiB；`/tmp/opt` 指向该盘的 `entware`，原有 `/opt -> tmp/opt` 保留。禁止重复格式化/安装 Entware。
- Entware、Python 3.13.9、ffprobe 6.1.4 和 curl 8.15.0 均已安装。探针安装自 master `17a8032a3a8f806b0ad94efdb96926bddd2cae6d`，不是安装仍在进行。
- 原有 Koolshare 的 post-mount/unmount 调用已保留，后面追加了该卷 Entware 的启动/停止；备份在 U 盘 `post-mount.before-iptv`、`unmount.before-iptv`。已做 sh -n，未冷启动验收。services-start 原有 Koolshare 和 tcp-tune 调用保留，已有 02:00/13:00 两项 cron。
- 现场修复：清除 LD_LIBRARY_PATH/LD_PRELOAD 后 GLIBC_2.25 错误消失；所有探针 *.sh 已加清理。空后缀 probe_id 已改为 `home-ac86u-8f8908f0fba9`，不得重新生成这个已确定编号。本提交把这些兼容修复用于后续安装，已存在配置仍保留。
- 现场下载曾超时；安装脚本已手动加入 IPv4、retry-all-errors。这些下载兼容选项仅用于安装文件，不代表家庭探测应该禁用 IPv6。
- 当前仍为 shadow/local queue only，publisher blocked，0 份报告，latest/state/github-state 尚缺失；没有完成 GitHub 配对或真实健康验收。不要单改 route_context 字符串冒充路径一致。
- Apple TV 当前使用 `https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u`，界面 425 台、41 组。当前本地仓库比对：53 个央视/省卫，江苏卫视 URL 与 tv-core 不同；需要获取最新清单再次核对，不能直接把 425 台列表塞给只接受核心台的探针。
- Apple TV 的 IPv4 是 192.168.50.167，网关及第一 DNS 为 192.168.50.1；还显示一条与当前 br0 前缀不同的 IPv6 DNS，原因未验证。
- `/etc/resolv.conf` 使用运营商 DNS；dnsmasq 则 `no-resolv`、`server=127.0.0.1#23453`。因此系统 DNS 与电视 DNS 未确认一致。
- 正在运行 `/koolshare/bin/clash -d /koolshare/merlinclash/ -f /koolshare/merlinclash/yaml_use/zhuli500m.yaml`。rule 模式、IPv6 true，redir 23457、tproxy 23458，没有查到 HTTP/SOCKS/mixed 入口。不能把 23457/23458 当 HTTP 代理端口。
- IPv4 PREROUTING TCP 进入 merlinclash，先绕过 ipset_proxyarround/direct_list，再到 merlinclash_CHN：ipset_proxy 或不在 china_ip_route 的地址重定向 23457。本机 OUTPUT 有按 mark 到 merlinclash_EXT 的规则，mangle OUTPUT 空。多线路应跟随现有规则与选组，不能固定某一个节点。
- 最新 IPv6 nat/mangle 只有默认 ACCEPT 内建链，没有额外规则；不能据此跳过 DNS、IPv6 可达性与播放器地址选择的验证。
- 临时 NETNS 测试返回 EINVAL，未创建 veth；owner 测试返回 `No chain/target/match by that name`，没有 OWNER: OK。脚本已执行临时测试链的清理。不要继续假定这两种办法可用，也不要为了它们刷固件。
- 新 `route_check.py` 为独立诊断，不被 cron/安装器自动启动。仅一个带 SO_MARK、限定目的 IPv4 的 HTTPS 连接进入现有 merlinclash 链，使用精确临时 OUTPUT 规则；退出/连接失败/受处理的中断后删除。不调用规则清空，不修改 Clash、订阅、probe 配置或发布状态。若清理失败打印精确删除命令。强杀/掉电不保证 finally 执行，测试仍不构成持久化方案。
- 该诊断当前使用系统 DNS，IPv4-only、仅 GitHub；**即使成功，也不证明家庭 DNS、IPv6、ffprobe 与电视等价**。待现场运行后再决定完整传输适配；不要声称适配已经完成。
- 本轮本地验证：7 项新诊断测试（成功/连接超时/中断/不支持 SO_MARK/既有规则/HTTP 错误/清理失败）及安装、部署回归共 23 项通过；隔离安装实际执行两次，验证 UUID 格式和重复安装保留 ID；router/ac86u 所有 Shell 语法通过。网络与防火墙调用在诊断单测中模拟，不能当成路由器实测。未重复跑全仓 222 项历史基线。
- 晚高峰检测/次日凌晨处理晚间卡顿的用户新增规则尚未实现；晚上仅检测正式台、不换台不测备用，晚上确认差的旧源次日降至最低优先，白天恢复不自动洗白。建议过 20:30 时间，仍需准确记录最终时点。

### 当前下一步

1. 手机下载本提交的 `route_check.py` 到 `/tmp`，清除 LD_* 后执行，观察 SO_MARK / MARKED_HTTPS / TEMP_RULE 输出。
2. 若连接可用，再实现程序传输和 DNS 的完整适配，同时处理 ffprobe、IPv6、源地址分组以及真实 tv.m3u 的核心地址绑定；目前尚未做这些改动。
3. 核对开机挂载与运行环境；GitHub Ruleset/Deploy Key 配对仍待办，正式发布继续关闭。之后才开始不同时间、跨度至少 18 小时的 4 份有效家庭报告验收。
4. 提交断点只更新功能分支；此前 master 合并已完成，新代码未合入 master，不要把本次功能分支保存等同于正式部署完成。

## 早前合并历史状态

- 仓库：`pppaaasss/-`；功能分支：`home-first-ac86u`；报告分支：`home-reports`。
- 本次起始功能头：`b6b348255ee2124085ee0de68cc26911af06de70`。
- 初次 master 同步：`8ddfac295a76176f2b8b5be7f1dfe0d0ff95e8f3`，合并提交 `4643fd9887ad17e6846814502e3ebb4c75de3d16`。
- 额度中断后再次同步 master：`c6fea1089af6730dbaf4323739d6b7fc258bf7e3`，合并提交 `647b89c4ac0b3915618bba98b835e406f2017d3a`。
- 本文件所在的部署修复提交是本轮代码断点；不要把上述同步提交误当作最新功能头。必须重新获取远端 SHA。
- 已修复安装漏文件、影子配对死循环、UNKNOWN 误判、凌晨顺序和预算、下午无备用测试、观看反馈和真实影子报告计数。
- 全仓 222 项：205 通过、6 失败、11 报错；家庭专项 80 项通过。17 项失败/报错与共同起点逐项一致。详见测试报告及 `test-evidence-2026-09-05.json`。
- 四份正式台单、当前候选及采集记录均逐字节保留最新同步 master。没有重新生成、回滚或主动修正正式台单。
- `config/home-publisher.json` 仍为 `enabled=false`、`expected_probe_id=""`。
- **已获得合并确认且 PR #5 已合入 master；尚未操作家庭路由器，尚未进行 U 盘安装或真实影子验收。**

## 用户最新规则（覆盖原交接提示词）

- AC86U 家庭实际路径是央视和省卫唯一健康裁决；GitHub 只搜集候选文本、读取报告和发布。香港不检测、不转发、不参与替换。
- 北京时间 00:30 GitHub 搜集候选；02:00 家庭主检正式台、维护/验证备用；02:25 GitHub 读取报告。
- **13:00 只检查正式台的播放、速度和清晰度，需要换源时直接引用仍有效的凌晨家庭备用证据。下午不测试备用，换前也不复验。13:25 GitHub 读取报告。**
- 废除三天轮换和每六小时检测，没有最多六台限制。好线路不动，UNKNOWN 永不替换。电视实测卡顿、模糊、串台优先。
- 重点保障爸妈常看的央视和省卫，先处理正式台故障，再补备用；不堆无关防御工程。
- 用户只有手机；现场 SSH 一次一个命令或一屏步骤，等用户贴结果后再继续。
- 用户正在看电视，已告知当前仓库同步、代码修复与离线测试不操作路由器和正式订阅。U 盘/Entware 现场操作前先说明可能的影响。

## 早前待办（已完成的安装项以最新现场节为准）

1. 修复提交 `1af1c5e49646c26b383177234d5de749e94e3129` 已在远端并经 PR #5 合入 master。续做先读最新交接文档和分支，再进行用户插盘后的只读检查。
2. 获取实时 master。旧任务仍可能增加候选、改变 `tv.m3u`/`tv-all.m3u`；若更新，继续合入功能分支并保留全部新数据，验证不增加原有 17 项问题。
3. 合并确认及 PR #5 合并已经完成，不要重复要求用户确认这次合并。发布配置继续保持关闭。
4. 按安装文档设置/核验 master Ruleset 与 Actions PR 权限。审计时规则集为空；不能假设已经设置。
5. 再带用户确认 64 GB USB 2.0 目标盘、清空授权、ext4、Entware；从已验收 master 安装探针，保留影子模式。
6. 核对实际订阅和家庭路径。easy 版有 13 个卫视地址与 core 不同，缺东方卫视且深圳卫视重复一次；不在影子前擅自调整。
7. 配对报告 Deploy Key；发布关闭和编号为空也能进行受保护的影子上传。路径确认后收集至少 4 份不同时间且跨度 18 小时的家庭报告，不能用同一报告重传凑数。
8. 用户验收影子后再用独立提交填真实 probe ID、启用发布器。

额度不足时优先更新本文件并提交功能分支，写明已推送的断点、测试结果、下一步及剩余阻塞，不仅在聊天里口头交接。
