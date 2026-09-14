# AC86U 最小采样版

路由器不再运行 Python、FFprobe、Git、历史数据库或 HTTP 代理服务。运行包只有一个 26,536 字节原生程序和三个 Shell 脚本，合计约 35 KiB（文件大小，非运行内存），复用现有 Entware curl/libcurl。SHA-256 校验需要 `sha256sum`；缺少时安装器通过 Entware 单独安装 `coreutils-sha256sum`，其文件及依赖不包含在上述 35 KiB 内。Python 只在手机准备迁移时及 GitHub Actions 中运行。代码默认关闭，提交或合并不会安装到家里的路由器。

## 路由端预算

| 项目 | 行为 |
| --- | --- |
| 空闲 | 无常驻采样进程；cron 每 5 分钟唤醒，窗口外直接退出 |
| 并发 | 逐条采样，每条间隔 2 秒；现用源一批最多 4 条，候选最多 2 条 |
| 内存保护 | 启动需可用 58 MiB；低于 50 MiB 停止；采样进程组与监护进程 RSS 合计超过 16 MiB 停止 |
| CPU | `nice 19`；系统忙碌度达到 75% 时不启动，运行中连续两次达到门槛则暂停 |
| 时长 | 每批整个进程组最多约 240 秒，包含控制请求及上传；清理另需约半秒 |
| 视频缓冲 | libcurl 16 KiB 流式读取；仅保留每条第一段的前 128 KiB；不累积整段视频 |
| 本地断点 | 一个待上传批次，不保存媒体历史；已完成的每条记录原子落盘 |
| 云端预算 | 每日采样预算 1 小时、1.5 GiB；新增最多 10 条，另限 600 秒、256 MiB；重启不会清零 |

这些是提前停止门槛，**不是 cgroup 内存硬限制或 AC86U 的实测峰值**。每 250 毫秒检查进程组，短暂峰值和内核/TLS 缓冲仍可能超出采样值。资源优先于完成率，不承诺每天能测完全部频道。带 VPN 的 AC86U 必须先验收一批。

每日时间预算按采样记录结算，不含批次前后的 GitHub 控制和上传时间；240 秒进程组保护仍覆盖它们。读取预算统计应用层视频数据，不含 DNS、HTTP/TLS 开销、重传和 GitHub 上传。每批最多两次/条、6 MiB/次的采样，加上有界播放列表读取，低于云端预留的 64 MiB。中断批次不退还未记录的资源预留，避免断电让用量清零。

## 家庭测量仍在家里

原生程序向 `192.168.50.1` 查询 A/AAAA，拒绝换用其他 DNS。IPv4 使用该批次专属 SO_MARK 进入原 `merlinclash` 链，IPv6 保持直连。退出只清理自己的标记规则；清理失败会留下 `PAUSED`，不继续测速。API 请求使用原有普通出站路径。

云端只解码家中已采到的短样本，不从 GitHub 服务器请求电视源。短样本暂存在需要认证的草稿 Release 中，解析后的测量 JSON 和 SHA-256 持久保存到 `home-reports`，成功推送报告及控制状态后删除临时样本，媒体字节不进入 Git 历史。上传断线时原文件重试，不重新测速。

保留原规则：两段样本、1.05 倍速度余量、两次明确失败才判 BAD、UNKNOWN 不换源、观众反馈否决、永久已测去重、历史备份保留、下午仅现用源检查、完整频道覆盖和四份正式订阅的原发布保护。

**格式边界：**普通 MPEG-TS HLS、HTTP TS/FLV 可采样；加密 HLS、字节范围分片、fMP4 初始化段和 LL-HLS 目前记为 UNKNOWN。短样本无法可靠获得画质或码率时也记 UNKNOWN。不会为了低占用把无法判断的源写成合格或故障。

## 一次验收，再决定是否定时

以下均在手机 Termux 操作。无需修复路由器的 Python，也不卸载旧包。先保留历史，包括 `state.json.corrupt-*` 原件；这些隔离文件可能受旧 Python 故障影响，必须在手机上验证，不能直接删除或创建空历史绕过迁移。

1. 审查合并代码，保持 `config/home-thin.json` 的 `enabled: false`。用有到期日、仅此仓库 `Contents: Read and write` 的 token，存入手机 `0600` 文件；已有此文件可复用。不要将令牌发到聊天或放进命令历史。
2. 手机需先有 Python（`apt-get install --no-install-recommends python`）。用审查通过的 **40 位提交 SHA** 下载入口。入口校验二进制和源文件摘要，手机准备配置；路由器只执行 Shell 和原生程序。安装器先检查 `sha256sum`，缺少时执行 `opkg update` 和 `opkg install coreutils-sha256sum`，通过空文件已知摘要自检后才进入迁移。它会备份并冻结旧 IPTV 任务，保留其他启动服务和所有历史，新任务保持关闭。

```sh
iptv_ref=这里填写审查通过的40位提交SHA
curl -fSL "https://raw.githubusercontent.com/pppaaasss/-/$iptv_ref/router/ac86u/thin_from_termux.sh" -o "$HOME/iptv-native-install.sh"
sh "$HOME/iptv-native-install.sh" "$iptv_ref" "$HOME/iptv-thin.token"
```

期望 `NATIVE_STAGED_DISABLED`。只发送约几十 KB 的运行代码；交叉编译、源码和 Python 安装脚本不装进路由器运行目录。手机为每次操作建立一个临时 SSH 主连接，复用登录，结束时关闭；中途断线会报错，不反复询问密码。失败直接显示最多四行原因，无需截取 Python 异常堆栈。

若存在活跃旧 worker、错误架构、缺失现有 libcurl、路径合约不符或已有迁移备份，暂存会停止。除按需补齐 `coreutils-sha256sum` 外，不升级现有包。遇到 `stage.sh: sha256sum: not found` 的旧入口失败，可下载修复提交重新暂存：该报错发生在创建迁移备份、冻结旧任务之前。

3. 先在手机执行 `mkdir -p "$HOME/iptv-thin-tools/router/ac86u" "$HOME/iptv-thin-tools/scripts"`，然后按 [历史导出与上传](home-thin-migration.md#3-导出和迁移历史) 的手机流程迁移原历史。导出脚本已支持原生版画质策略位置。完成后通过 PR 将云端 `enabled` 改为 `true`，确认 `home-control/status.json` 显示 `history_migrated: true`。
4. 在北京时间 02–08、13–16、20–23 的测量窗口内，手机执行一次：

```sh
python "$HOME/iptv-native-$iptv_ref/native_from_termux.py" once --token-file "$HOME/iptv-thin.token"
```

此命令结束会移除启用标记，不留下持续测速。查看 `UPLOADED`、`resources.txt`、VPN 和电视播放，并确认 GitHub 收到了新的家庭测量时间。`resources.txt` 依次为：停止原因、峰值 RSS（KiB）、最低可用内存（KiB）、运行秒数。`WAITING_RESOURCES`、`WAITING_CLOUD`、`WAITING_WINDOW` 均不代表完成测试；上传失败保留断点，可再次执行 `once`。实机结果不足以支持 16 MiB 门槛时，应继续精简，不能直接提高限额。

验收满意后启用同一套程序的定时任务：

```sh
python "$HOME/iptv-native-$iptv_ref/native_from_termux.py" enable --token-file "$HOME/iptv-thin.token"
```

随时暂停，不需要路由器 Python：

```sh
ssh -p 22 wodeluyouqi@192.168.50.1 'touch /opt/var/lib/iptv-home-native/PAUSED; cru d IPTVHomeNative; sh /opt/share/iptv-home-native/native_status.sh'
```

当前批次会在自己的预算内结束，不强杀 VPN。恢复前先检查 `resources.txt`；尤其 `ROUTE_CLEANUP_FAILED` 必须处理自己的遗留标记。确认正常后删除原生数据目录的 `PAUSED`，再从手机 `enable`。

若需要恢复旧测速计划，先关闭云端派发，并确认旧 Python 故障已修复和旧版负荷可接受，再运行手机入口的 `rollback`。它取得同一组锁，恢复备份中的旧配置、IPTV 启动块及 cron；其他启动服务的后续修改保留，所有旧历史及新断点保留。若只是需要省资源，保持暂停即可。

## 开发验证

GitHub `Build minimal AC86U sampler` 在 Ubuntu 20.04 容器中交叉编译 aarch64 ELF，检查依赖最高不超过 GLIBC 2.27，运行包 SHA-256 绑定源文件。回归测试覆盖实际 HTTP 重定向/忽略 Range 的读取上限、LAN DNS、离线画质解析、完整受保护发布、进程组中断、丢样 UNKNOWN、预算和历史流程。AC86U 的 libc/libcurl 兼容性、SO_MARK 权限及 VPN 实际路径由首次现场验收确认。

接口依据：[GitHub Release assets](https://docs.github.com/en/rest/releases/assets)、[libcurl socket callback](https://curl.se/libcurl/c/CURLOPT_OPENSOCKETFUNCTION.html)。
