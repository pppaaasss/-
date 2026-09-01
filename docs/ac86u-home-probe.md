# RT-AC86U 家庭电视健康探针

> **离线代码和贯通模拟已完成，暂勿在家中安装。** 等用户出院回家、U 盘插入 AC86U，并把已验收功能分支合入受保护的 `master` 后再现场部署。实施进度见 [`home-first-roadmap.md`](home-first-roadmap.md)。

AC86U 是央视和省级卫视健康度的唯一检测与裁决位置。GitHub 负责搜集未验证候选和受保护地发布正式清单；香港主机不检测、不转发、不参与替换。整套任务在路由器上定时运行，不依赖 ChatGPT/Codex 在线，也不受对话额度影响。

## 固定运行规则

- 北京时间 `02:00`：检查全部正式央视和省级卫视；维护即将过期的家庭备用；拉取 GitHub 当日新增或变更候选并在家庭实际路径逐项验证。
- 北京时间 `13:00`：只检查全部正式央视和省级卫视；仅当正式线路确认不合格时，快速复验对应的家庭合格备用。
- 好线路保持原样，不做三天轮换；`UNKNOWN` 永不触发替换；同一轮没有“最多替换几台”的业务上限。
- 当前线路须在同一轮家庭路径连续两次不合格，才记为 `BAD`。一次失败后恢复只记为 `UNKNOWN`。
- 候选须在家庭路径通过两个不同视频分片、下载余量、真实分辨率、编码和最低节目码率检查，才能进入 36 小时家庭备用池；正式切换前还要复验。
- 用户在电视上确认的卡顿、模糊、串台或错误频道拥有最高优先级，可否决自动结果。

## 资源与故障保护

- 单线程运行，使用 `nice -n 15`；有 `ionice` 时同时使用最低 I/O 优先级。
- 1 分钟负载高于 1.5、可用内存低于 64 MiB 或上一轮仍在运行时，本轮跳过。
- 单轮总预算 3300 秒。没有跑完的候选保存在 U 盘队列，下一次 `02:00` 续跑；不会为了赶完压垮 AC86U。
- 正式线路通常读取两个各不超过 2 MiB 的分片；只有第一次不合格才完整复测。候选读取两个各不超过 6 MiB 的分片并调用 `ffprobe`，不做转码。
- 家庭断网、GitHub 不可达、路径不一致、报告不完整或大面积同时异常都会保持原正式清单；至少 12 台同时未知且达到比例阈值时整轮熔断。
- 状态、队列和日志位于 U 盘 `/opt`。U 盘掉线时任务不运行，不影响原有上网、分流或电视订阅。

## GitHub 直连边界

- 路由器只主动连接 `ssh.github.com:443`，家里不开放入站端口，也不经过香港中转。
- 不保存 GitHub 账号令牌。安装器生成独立 Ed25519 Deploy Key，私钥权限固定为 `0600`。
- SSH 主机密钥必须与 GitHub 官方 Ed25519 指纹 `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU` 完全一致；不允许首次连接自动接受未知主机。
- 代码把仓库锁定为 `pppaaasss/-`、报告分支锁定为 `home-reports`。报告先在本地完成版本、探针编号、内容哈希、大小和正式清单绑定校验，再写入 `inbox/<probe_id>/`。
- GitHub 推送失败时，报告原样留在 U 盘并在下一轮重试；失败本身不能修改正式清单。
- `home-reports` 是独立报告入口，不保存正式清单。路由器报告只是证据，只有受保护的 GitHub 工作流可以更新四份正式播放清单。
- `github_pair.py --enable` 会先从 `master` 读取发布配置并核对探针编号，再通过无需账号令牌的 GitHub 公共 Rules API 检查当前生效规则：同一条仓库规则集必须同时要求 PR、0 人工审批、禁止强推、禁止删除并允许 squash；任何一项不满足都不会开启写密钥。

重要限制：GitHub 的可写 Deploy Key 是仓库级凭据，并非分支级凭据。脚本会拒绝推往 `master`，但私钥一旦被盗，攻击者不会受脚本约束。因此必须先在 Phase 5 完成默认分支保护和受保护发布工作流，现场部署时才允许给 Deploy Key 勾选写权限并运行 `--enable`。Phase 5 未就绪时，安装配置中的硬门槛会让推送和正式激活都失败关闭，只保留 U 盘队列。

### `master` 一次性保护设置

这里使用 GitHub **Ruleset**，而不是依赖需要 Administration 权限才能读取明细的旧 Branch protection API。代码只读取 GitHub 对公开仓库开放的“某分支当前生效规则”接口，AC86U 因而不需要保存 PAT。

1. 仓库 `Settings` → `Rules` → `Rulesets` → `New branch ruleset`，名称设为 `home-production-master`，状态设为 `Active`，目标只包含 `master`。
2. `Bypass list` 必须为空；尤其不要加入仓库管理员、GitHub Actions 或任何 Deploy Key。公开 Rules API 不返回隐藏的绕过名单，因此这一项必须在现场由用户人工确认一次。
3. 开启 `Restrict deletions`、`Require a pull request before merging` 和 `Block force pushes`。PR 规则中审批数设为 `0`，关闭 Code Owner、最后推送者之外审批和会话解决要求，并允许 `Squash`。
4. 仓库 `Settings` → `Actions` → `General`：Workflow permissions 设为 `Read and write permissions`，并开启 `Allow GitHub Actions to create and approve pull requests`。这里不要求 Actions 实际审批，只是允许它创建每日自动 PR。
5. 每日采集和家庭发布工作流都只推送 `automation/...` 临时分支，再创建并 squash 合并 PR；代码中没有直推 `master`。规则集未生效时，采集门禁失败；家庭发布配置未启用时，发布器只返回 `disabled`。
6. `config/home-publisher.json` 目前故意保持 `enabled: false` 且探针编号为空。只有 U 盘安装后取得真实 `probe_id`、规则集已核验且影子报告通过，才在单独提交中填入编号并启用。

## 64 GB USB 2.0 到货后的现场顺序

以下步骤现在只作为准备清单，等出院回家后执行。格式化会清空 U 盘，先确认盘里没有要保留的数据。

1. 把 U 盘插到 AC86U，在梅林中运行 `amtm`。
2. 使用 `fd` 把 U 盘格式化为 `ext4`，再安装 Entware；确认 `/opt/bin/opkg` 存在。USB 2.0 对这类低速、零转码、顺序写小状态文件的任务足够，发热和兼容性通常也比高速盘更省心。
3. 确认 Phase 5、Phase 6 和模拟测试均已完成，再从已验收的 `master` 安装。安装命令将在现场部署前做最后一次核对；不要提前从功能分支安装。
4. 手机连接家中 Wi-Fi，通过仅限局域网的 SSH 登录 AC86U；不需要电脑。功能分支合入 `master` 后，在路由器执行：

   ```sh
   curl -fL --retry 3 \
     https://raw.githubusercontent.com/pppaaasss/-/master/router/ac86u/install.sh \
     -o /tmp/iptv-home-install.sh
   IPTV_HOME_REF=master sh /tmp/iptv-home-install.sh
   ```

5. 安装后仍为本地影子模式，不改路由、不改播放清单，也不向 GitHub 发报告。查看状态和 Deploy Key 公钥：

   ```sh
   /opt/share/iptv-home-probe/status.sh
   /opt/share/iptv-home-probe/github_pair.py --show-key
   ```

6. 先确认 `master` 已受保护，再在仓库 Settings → Deploy keys 添加这把公钥，并仅为这一把密钥开启写权限。随后由路由器核验 GitHub 指纹、认证并开启报告推送：

   ```sh
   /opt/share/iptv-home-probe/github_pair.py --enable
   ```

7. 现场核对 AC86U 自身请求与 Apple TV 的 DNS、出口、ShellClash/Mihomo 分流、IPv4/IPv6 选择一致。只有确认等价后才标记家庭路径；这一步仍保持影子模式，并立即生成一份 `13:00` 类型报告：

   ```sh
   /opt/share/iptv-home-probe/activate.sh --confirm-living-room-path
   ```

8. 至少取得 4 份成功进入 GitHub 的报告、跨度达到 18 小时，且最新报告新鲜、待发队列为空、熔断器关闭后，才允许正式激活：

   ```sh
   /opt/share/iptv-home-probe/activate.sh
   ```

## 日常查看、停用与恢复

查看状态、实际待发数量和最近日志：

```sh
/opt/share/iptv-home-probe/status.sh
```

关闭生产影响但保留检测和影子报告：

```sh
/opt/share/iptv-home-probe/activate.sh --off
```

关闭 GitHub 推送但保留本地报告队列：

```sh
/opt/share/iptv-home-probe/github_pair.py --off
```

卸载代码和两条定时任务、保留配置、密钥和历史以便恢复：

```sh
/opt/share/iptv-home-probe/uninstall.sh
```

彻底删除配置、Deploy Key 私钥和 USB 历史只能显式执行：

```sh
/opt/share/iptv-home-probe/uninstall.sh --purge
```

卸载不会删除 Entware 包，也不会改 ShellClash/Mihomo、DNS、透明代理、防火墙或电视订阅。若 Deploy Key 不再使用，还必须在 GitHub 仓库设置中单独撤销。

## 自动检测的能力边界

探针能判断持续打不开、卡顿余量不足、分辨率不足、H.264/H.265 实际节目码率偏低，以及备线在家庭网络是否真的可用。仅靠网络和视频元数据不能可靠识别“CCTV-15 实际播成广东卫视”这类内容串台；此类语义错误始终以用户实际观看反馈为最高优先级。
