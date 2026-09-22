# AC86U 临时规则清理修复（2026-09-22）

## 已确认的现场问题

家庭端显示 `ENABLED`、`PAUSED`、`ROUTE_CLEANUP_FAILED`；状态文件最后一次
写入为 `WAITING_WINDOW`。可用内存约121 MiB，cron仍每30秒调用原生入口。
现场 OUTPUT 列表没有显示 `0x4960xxxx` 测速规则。

旧 guardian 在所有任务退出后都执行 `iptables -C`，即使 worker只读取了
IDLE任务、完全没有插入规则。规则检查返回大于1或清理失败时写入空的
`PAUSED`，以后cron直接退出；iptables错误又被重定向到 `/dev/null`。
这足以造成一次本地操作异常后长期停止。现场无法还原当时具体的iptables
错误，不能断言是锁冲突或固件不支持某个选项。

## 本次行为

- worker插入规则前原子写入本批 `route-mark`；没有此文件就不检查防火墙。
- guardian先处理遗留的本批标记，再允许新的worker运行；结束时也检查该标记。
- 清理通过成功的 `iptables -t nat -S OUTPUT` 输出确认规则存在或已经消失。
  只删除完整匹配本批mark、TCP、OUTPUT、merlinclash的规则，保留其他代理规则。
- 每条清理命令最多2秒，失败进行有限重试。仍失败时保留mark并报告
  `ROUTE_CLEANUP_RETRY`，下次cron先重试清理；不再因此新建永久 `PAUSED`。
- `route-error.txt` 保留最近一次清理的时间和stderr；状态入口显示待清理mark与错误。
- 显式修复入口仅在旧暂停原因为 `ROUTE_CLEANUP_FAILED` 且成功检查不到
  测速规则时解除旧暂停；其他暂停原因或无法确认规则状态时停止恢复。

## 手机更新

使用最终审查提交SHA下载 `router/ac86u/native/repair_from_termux.py`，在
Termux运行 `python repair_from_termux.py <40位SHA>`。手机校验ARM二进制、
源文件及manifest摘要，再通过一次SSH登录上传三个运行文件。

更新持有原有worker锁，先备份到
`/opt/var/lib/iptv-home-native/before-route-retry-<提交前12位>`，通过同文件系统
临时文件原子替换。写入或校验失败会尝试还原。旧启用标记、现有cron、
采样窗口、资源限额、上传断点和正式播放清单均沿用已有配置。
远端临时目录使用独占mkdir，不依赖AC86U没有的mktemp。

看到 `ROUTE_REPAIR_OK` 表示文件更新与暂停处理完成。之后必须核对新家庭
测量时间才能确认采样和上传恢复；云端构建成功不能替代实机验收。

## 验证

新增原生回归覆盖空闲任务、规则已消失、精确删除、列举失败后下一轮恢复、
删除失败与竞态、worker被中断、错误mark、命令超时、旧暂停恢复条件。
手机入口测试执行实际Shell安装事务和写入失败还原。原生HTTP/预算/发布测试
继续运行；ARM产物由现有Ubuntu20.04工作流交叉编译并经QEMU哈希向量校验。
