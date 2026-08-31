# RT-AC86U 家庭电视健康探针

> **家庭优先架构迁移中，暂勿按本页安装。** 本页以下内容仍记录旧的香港中转、每 6 小时和三天轮换方案，仅供迁移时核对回退依据。新方案固定为北京时间 `02:00` 主检、`13:00` 正式线路复检，检测与裁决全部在家中完成；完成影子测试后会整体替换本页。当前进度见 [`home-first-roadmap.md`](home-first-roadmap.md)。

这套东西已经按“U 盘到货后直接部署”准备好，但目前没有远程改动家里的路由器。它解决的是香港测速和客厅实际体验不一致的问题：AC86U 在家里定时抽取央视、卫视的真实视频分片，把经过校验的报告交给香港 VPS；三天轮换只有在同一条正式线路和家庭备线都被反复验证后才会参考这份报告。

它不依赖 ChatGPT/Codex 在线，也不受 5 小时额度影响。路由器上没有 GitHub 令牌，不能修改仓库和正式清单。

## 已锁死的安全边界

- 每 6 小时一次轻检，单线程，每台读取两个不同分片，每段最多 2 MiB。
- 每 72 小时最多一次深检，每段最多 12 MiB，并用 `ffprobe` 读取分辨率、编码、帧率和码率；不运行 `ffmpeg` 转码，不解码画面。
- 进程使用 `nice -n 15`；1 分钟负载高于 1.5 或可用内存低于 64 MiB 时本轮自动跳过。
- 单轮总预算 3300 秒；使用原子目录锁，上一轮未结束时不会叠跑。
- 连续 3 轮且跨度至少 6 小时才确认家庭死亡或降级；大面积同时失败会打开熔断器，不累计坏线次数。
- 家庭坏线的备线也必须在家里完成两次深检、跨度至少 6 小时且保持 GOOD，才能进入三天轮换。
- 报告绑定正式清单 SHA-256 和线路 URL；清单或地址变过，旧报告自动作废。
- 首次配对后至少 4 次成功上传、跨度至少 18 小时，仍只处于影子模式。没有显式激活就永不参与生产决策。
- 家庭报告只进入三天统一轮换，不放宽香港的 6 小时“确认死亡才切换”规则，也没有任何频道数量上限。
- 所有状态和日志都在 U 盘 `/opt` 下。JFFS 只增加一个很小的 Merlin `services-start` 定时任务登记块。

理论流量上限：约 58 台时，一轮轻检最多约 232 MiB，一天约 0.9 GiB；三天一次深检最多约 1.36 GiB。大多数分片小于上限，实际通常更低。备线只在对应正式线路已经确认不合格时测试，不会把整套候选池常年扫一遍。

## U 盘到货后的顺序

以下操作等出院后再做。格式化会清空 U 盘，确认盘里没有要保留的内容。

1. 把 64 GB USB 2.0 U 盘插到 AC86U。
2. SSH 登录梅林，运行 `amtm`，用 `fd` 把 U 盘格式化为 `ext4`。不要选 NTFS/exFAT，也暂时不要建交换分区。
3. 继续在 `amtm` 安装 Entware，确认 `/opt/bin/opkg` 存在。
4. 先只安装本地影子探针：

   ```sh
   IPTV_HOME_SKIP_INITIAL_RUN=1 sh -c "$(curl -fsSL https://raw.githubusercontent.com/pppaaasss/-/master/router/ac86u/install.sh)"
   ```

5. 查看探针编号和专用公钥：

   ```sh
   /opt/share/iptv-home-probe/status.sh
   /opt/bin/python3 /opt/share/iptv-home-probe/pair.py --show-key
   ```

安装器只安装 Entware 的 `python3`、`ffprobe`、`curl`、CA 证书和 OpenSSH 客户端组件；不会改 ShellClash/Mihomo、DNS、透明代理端口、防火墙或电视订阅。

## 配对香港 VPS

先在香港 VPS 的现有仓库目录执行，参数必须使用路由器刚显示的实际探针编号和整行 `ssh-ed25519` 公钥：

```sh
cd /opt/iptv-hk-probe
git pull --ff-only origin master
sudo bash scripts/install_home_probe_receiver.sh \
  --probe-id '实际探针编号' \
  --public-key '路由器显示的整行 ssh-ed25519 公钥'
```

安装结束会显示 VPS 的 Ed25519 主机指纹。回到路由器，用实际 VPS 地址、SSH 端口和刚显示的 `SHA256:...` 指纹配对：

```sh
/opt/bin/python3 /opt/share/iptv-home-probe/pair.py \
  --host '香港VPS地址' \
  --port 22 \
  --fingerprint 'SHA256:实际指纹'
```

指纹不一致时脚本会硬失败，不会接受“先连上再说”。路由器专用密钥在 VPS 上只能执行报告接收命令，不能取得 shell、端口转发、PTY 或代理权限。

开始第一次轻检并查看结果：

```sh
/opt/share/iptv-home-probe/run.sh --mode light
/opt/share/iptv-home-probe/status.sh
```

## 影子期与正式启用

定时任务在路由器本地时间 `00:07、06:07、12:07、18:07` 运行。先让它跑满至少 24 小时。期间报告会上传，但 `actionable=false`，GitHub 轮换明确忽略它。

AC86U 自身发起的连接不一定天然经过 Apple TV 完全相同的 ShellClash/Mihomo 路径。因此正式启用前必须现场核对当前活动配置和分流命中，不能盲目点开。普通激活命令会因路径未核验而拒绝；确认两者等价后才运行：

```sh
/opt/share/iptv-home-probe/activate.sh --confirm-living-room-path
```

激活脚本还会检查至少 4 次上传、18 小时跨度、最新报告新鲜且熔断器关闭。任何一项不满足都会保持影子模式。激活后也要等下一份 `actionable=true` 报告，三天轮换才可能使用它。

## 日常查看与停用

查看状态和最近日志：

```sh
/opt/share/iptv-home-probe/status.sh
```

只取消生产影响、继续留作影子观测：

```sh
/opt/share/iptv-home-probe/activate.sh --off
```

暂停定时任务：

```sh
cru d IPTVHomeProbe
```

卸载代码和定时任务、保留密钥/配置/历史以便恢复：

```sh
/opt/share/iptv-home-probe/uninstall.sh
```

彻底清除密钥、配置和 USB 历史，应在卸载代码前执行：

```sh
/opt/share/iptv-home-probe/uninstall.sh --purge
```

卸载不会移除 Entware 包，也不会动现有分流。如果 U 盘掉线，`/opt` 下的程序不会运行，路由器原本的上网、分流和电视订阅仍保持原状。

## 能做与不能做

探针能判断打不开、持续卡顿余量不足、分辨率不足、H.264 实际节目码率偏低，以及备线在家庭网络是否真的可用。它不能仅靠网络字节可靠识别“CCTV-15 实际播成广东卫视”这类内容串台；这种语义错误仍以人工观看反馈为最高优先级，精确 URL 和重复失败主机黑名单继续生效。
