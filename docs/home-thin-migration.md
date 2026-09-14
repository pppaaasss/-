# AC86U 家庭测速减负迁移（旧 Python 版本留档）

> 当前部署改用 [AC86U 最小采样版](home-native-minimal.md)，路由器不再运行 Python 或 FFprobe。本文旧安装、修复和启用命令不再适用；仅第 3 节历史导出/上传流程继续复用。新入口需要第二个参数：手机 token 文件。

此版本把任务队列、候选去重、历史备份池、判定和 Git 发布放到现有 GitHub 项目。路由器只领取小任务、经原家庭路径采样、上报小结果。`config/home-thin.json` 默认关闭；提交代码不会自动安装或启用路由器。

## 已知故障和边界

现场截图显示 Entware Python 3.13 导入 `keyword` 时发生 `bad magic number`，隔离环境后仍然失败。`keyword.pyc` 被列出而对应源码未列出。这证明运行环境有问题，尚不能确定是包版本、文件损坏还是存储原因，也不能据此断定 CPU 负荷造成了损坏。

现有路由器最近一次记录的可用内存约 62 MiB。新配置要求开始测量时至少有 96 MiB，并在测量中低于 64 MiB 时停止。停掉旧任务后必须重新测空闲内存；若仍不足，新程序会一直暂停，不能通过降低门槛冒充恢复。减少缓冲和本地业务处理的实际收益，需要 AC86U 上的运行数据验证。

## 职责和证据

| 位置 | 负责内容 |
| --- | --- |
| GitHub master | 受审查的代码、正式频道快照、用户反馈、启用开关 |
| GitHub home-control | 完整历史、永久候选去重、队列、资源回执、最多 4 条的当前任务 |
| AC86U | 原 LAN DNS、SO_MARK 和 merlinclash 家庭路径；逐条下载两段并获取 FFprobe 元数据 |
| GitHub home-reports | 不可覆盖的原始测量、迁移分块、按原 v2 合约生成的报告 |
| 现有发布流程 | 校验完整正式频道集、时效、精确 URL 和反馈绑定，再通过 PR 更新四份订阅 |

路由器不克隆 Git 仓库、不载入历史备份池、不写完整家庭报告。保留 Python 和 FFprobe，只调用原采样与传输函数；部分兼容模块仍会导入，但不会读取或计算旧历史。

上传成功后通过固定 `repository_dispatch` 通知 master 的流程继续。事件不带任务命令；云端只执行 master 上的代码并验证数据。原来 15 分钟的定时检查负责通知丢失后的补查。GitHub 排队、家庭资源不足或窗口结束都可能造成这一轮不完整；不完整的正式频道检查不会用于换源。

云端报告使用最早一次实际测量时间，重试不刷新证据日期。`UNKNOWN` 不换源，现用源连续两次明确失败才成为 `BAD`，全网故障熔断保持生效。下午仅检查现用源，允许使用未过期的夜间/晚间家庭备份证据。试验分支历史仅供保留和重测，不直接成为下午的合格备份。用户明确认可的现用 URL 保留原画质例外，速度余量仍要求 1.05。

## 初始资源预算

| 项目 | 默认限制 |
| --- | --- |
| 并发 | 一个 worker，每批逐条测量；最多 4 条现用源，最多 2 条备用/新增源 |
| 批次 | 240 秒采样预算、64 MiB 上游读取预算 |
| 每日 | 3600 秒、1.5 GiB；进程重启不会清零 |
| 新增候选 | 每日最多 10 条，另有 600 秒、256 MiB 预算 |
| 内存 | 启动前可用至少 96 MiB；运行中保留 64 MiB；自身与子进程 RSS 目标上限 64 MiB |
| CPU | 开始前采样一秒，忙碌度达到 75% 就等待；以 `nice -n 15` 运行 |
| 本地数据 | 96 KiB 以下任务/结果；一个待上传批次、最多四条断点；原历史另行保留 |

视频读取以 64 KiB 分块计数，不累计整段视频。读取预算也覆盖经代理的 FFprobe HTTP/TLS 数据，包含 HTTP/TLS 开销；不计 DNS 小请求、内核缓冲和重传。每日视频额度不含少量 GitHub 控制请求。内存监控每 0.5 秒采样，属于提前停止机制，并非 cgroup 硬隔离；批次清理和网络超时可能使进程结束比 240 秒晚一些。任何本地资源中断都不记录为频道失败，也不把未完成的候选标成已测。

北京时间任务窗口为 02:00–08:00、13:00–16:00、20:00–23:00。唤醒脚本在其他时段不启动 Python，已有待上传结果除外。无法保证三个窗口每天都能完成所有频道；健康的家庭网络优先于测速完成率。

## 1. 先恢复运行环境，并保留损坏现场

所有以下 SSH 命令均在手机 Termux 执行。先查看，不安装或删除包：

```sh
ssh -p 22 wodeluyouqi@192.168.50.1 'sh -s' <<'SH'
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
/opt/bin/python3 -I -S -B -c 'import sys; print(sys.version); print(sys.executable)'
/opt/bin/opkg search /opt/lib/python3.13/keyword.pyc
/opt/bin/opkg list-installed | grep -E '^(python3|libpython3)'
ls -l /opt/lib/python3.13/keyword*
df -h /opt
awk '/^(MemAvailable|MemFree|SwapFree):/ {print}' /proc/meminfo
dmesg | grep -Ei 'I/O error|EXT4-fs error|usb.*(reset|disconnect)|Buffer I/O' | tail -n 12
SH
```

先确定 `keyword.pyc` 所属包和版本、保存它的副本，再修复相应 Entware 包；不要删除整个 Python 库、清空历史或全量 `opkg upgrade`。本版本安装器不会进行包修复。环境必须能通过：

```sh
ssh -p 22 wodeluyouqi@192.168.50.1 'unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; /opt/bin/python3 -I -S -B -c '\''import keyword,json,ssl,pathlib,threading,fcntl; print("PYTHON_OK")'\'''
```

截图中移走的 `pipeline-trial/state.json.corrupt-*` 需要单独恢复确认。它可能只是读取时受运行环境故障影响，文件本身是否损坏尚未确定。保留原件，在手机上用健康 Python 验证和比较时间戳，再恢复/合并完整记录；不能直接创建空 `state.json`。自动导出发现该隔离文件就停止。

## 2. 合并审查后的代码，暂存并冻结旧任务

先在 GitHub 合并减负代码，保持 `enabled: false`。手机需已安装 `curl`、`tar`、`python`、SSH。将 `iptv_ref` 替换为审查通过的 **40 位提交 SHA**，不要使用可变的 master 下载运行代码：

```sh
iptv_ref=这里填写审查通过的40位提交SHA
mkdir -p "$HOME/iptv-thin-tools/router/ac86u" "$HOME/iptv-thin-tools/scripts"
curl -fSL "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/thin_from_termux.sh" -o "$HOME/iptv-thin-tools/thin_from_termux.sh"
sh "$HOME/iptv-thin-tools/thin_from_termux.sh" "$iptv_ref"
```

期望 `THIN_STAGED_DISABLED`。安装器确认原家庭路径和发布保护已就绪，取得旧 worker/升级/新 worker 的锁，并保留 `/opt/var/lib/iptv-home-probe/before-thin-v1`。旧定时任务和启动块被替换，旧入口增加暂停标记；旧配置、入口、启动块和 cron 均留有副本。新代码装在独立目录，旧测速数据不删除。若已有任务运行，暂存会退出，等它释放锁后重试。

## 3. 导出和迁移历史

手机拉取同一个 SHA 的辅助文件：

```sh
for iptv_name in thin_export_from_termux.sh thin_api.py thin_contract.py; do
  curl -fSL "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/$iptv_name" -o "$HOME/iptv-thin-tools/router/ac86u/$iptv_name" || break
done
curl -fSL "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/scripts/upload_home_thin_history.py" -o "$HOME/iptv-thin-tools/scripts/upload_home_thin_history.py"
sh "$HOME/iptv-thin-tools/router/ac86u/thin_export_from_termux.sh" "$HOME/iptv-history-before-thin"
```

导出保留正式和试验状态、备份池、完整进度记录、指定画质参数。上传白名单不含路由器配置文件、SSH 密钥或令牌。历史原文压缩分块保存在 home-reports，云端校验大小和 SHA-256 后再导入。单文件最多 32 MiB，损坏或缺失必须先恢复。

需要为仓库 `pppaaasss/-` 单独创建有到期日的 fine-grained GitHub token，仓库权限仅 `Contents: Read and write`，不添加 Actions/Administration 权限。现有 SSH deploy key 不能直接用于 Contents API。令牌不要发送到聊天，也不要直接写进命令历史；在 Termux Bash 中隐藏输入：

```bash
umask 077
read -rsp 'GitHub token: ' iptv_token; printf '\n'
printf '%s\n' "$iptv_token" > "$HOME/iptv-thin.token"
unset iptv_token
chmod 600 "$HOME/iptv-thin.token"
python "$HOME/iptv-thin-tools/scripts/upload_home_thin_history.py" --directory "$HOME/iptv-history-before-thin" --token-file "$HOME/iptv-thin.token"
```

令牌的权限粒度是仓库级，不能单独限制到一个数据分支。程序只对 `home-reports/observations` 和 `migrations` 做不可覆盖写入；master 仍须受现有 PR 规则保护。保存令牌的文件要求 0600。

随后通过 PR 将 master 的 `config/home-thin.json` 中 `enabled` 改为 `true`，运行一次现有 Publish home-qualified IPTV decisions 流程或等待定时检查。查看 `home-control/status.json`，必须显示 `history_migrated: true`。回执 `cloud_queue_persisted_not_tested` 仅代表云端保存了候选，不能当作家庭测试完成。

## 4. 启用并验证实际负荷

```sh
ssh -p 22 wodeluyouqi@192.168.50.1 'unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; /opt/bin/python3 -E -s -B /opt/share/iptv-home-thin/thin_install.py activate'
ssh -p 22 wodeluyouqi@192.168.50.1 'sh /opt/share/iptv-home-thin/thin_status.sh'
```

期望 `THIN_ENABLED`，随后在测量窗口中看到 `UPLOADED`，云端收到新的实际测量时间。`WAITING_RESOURCES`、`WAITING_BUDGET`、`WAITING_CLOUD` 都是有效状态，不是测速成功。比较首批与停测后的可用内存、峰值 RSS、运行时长、流量和电视实际播放；只有这些现场证据才能说明路由器压力已降低。

即使 Python 故障也能停止后续唤醒并查看状态：

```sh
ssh -p 22 wodeluyouqi@192.168.50.1 'touch /opt/var/lib/iptv-home-thin/PAUSED; cru d IPTVHomeThin; sh /opt/share/iptv-home-thin/thin_status.sh'
```

这条命令不强杀正在清理的批次。当前批次受资源/时长预算约束，自行结束。运行环境再次损坏会写入 `runtime-failed`，修复并通过导入自检后才可移除该标记。

## 5. 回退

先在 GitHub 通过 PR 把 `home-thin.json` 的 `enabled` 改回 `false`，阻止继续派发。然后在路由器上恢复旧配置、入口和 IPTV 启动块；其他服务的启动修改保留：

```sh
ssh -p 22 wodeluyouqi@192.168.50.1 'unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH; /opt/bin/python3 -E -s -B /opt/share/iptv-home-thin/thin_install.py rollback'
```

回退要求 worker 已退出；锁占用时退出，稍后重试。旧历史和新测量全部保留。回退会恢复旧测速计划，因此应先确认 Python/存储故障已解决，并评估旧版负荷。若只想停测，使用上一节的暂停命令即可。不要删除任何 state 文件，也不要重装旧版覆盖本次迁移标记。

## 开发验证

```sh
python -m unittest discover -s tests -v
python scripts/audit_home_coverage.py
git diff --check
```

新增回归覆盖部分批次、资源暂停、预算持久化、上传丢失回执、任务过期、晚到重试、迁移损坏、试验历史隔离、家庭反馈例外和完整受保护发布。真实本地 HTTP/HLS/FFprobe 测试验证代理路径和计量。AC86U 实机迁移、Entware 修复和实际资源峰值尚待现场确认。

协议依据：[GitHub Contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)、[Repository dispatch 权限](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event)、[GitHub Actions 事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#repository_dispatch)。
