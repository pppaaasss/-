# 河南卫视换源（2026-10-03）
用户23:18:59北京时间要求替换。四份订阅统一更新同一官方线路的签名链接。
新地址：http://tvcdn.stream3.hndt.com/tv/65c4a6d5017e1000b2b6ea2500000000_transios/playlist.m3u8?wsSecret=771e28a9b84e09e6e6ffd6bcaa6c2d7d&wsTime=1790903091
原地址：
- tv-core.m3u: http://tvcdn.stream3.hndt.com/tv/65c4a6d5017e1000b2b6ea2500000000_transios/playlist.m3u8?wsSecret=10c032029347485d95a4ed7defdc22f5&wsTime=1789250696
- tv-easy.m3u: http://tvcdn.stream3.hndt.com/tv/65c4a6d5017e1000b2b6ea2500000000_transios/playlist.m3u8?wsSecret=01125a54dae615afaec759b33079de9c&wsTime=1787839130
- tv.m3u: http://tvcdn.stream3.hndt.com/tv/65c4a6d5017e1000b2b6ea2500000000_transios/playlist.m3u8?wsSecret=10c032029347485d95a4ed7defdc22f5&wsTime=1789250696
- tv-all.m3u: http://tvcdn.stream3.hndt.com/tv/65c4a6d5017e1000b2b6ea2500000000_transios/playlist.m3u8?wsSecret=10c032029347485d95a4ed7defdc22f5&wsTime=1789250696
依据：home-reports/inbox/home-ac86u-8f8908f0fba9/20261001T192532Z-primary-0200-36dc3da24f5e807b.json。
2026-10-02 08:04:45北京时间QUALIFIED，H264 1920×1080 50fps，下载108.789Mbps、流码率5.494Mbps、余量19.801倍。
截至换源已超过36小时，签名链接当前有效性未确认，按用户要求人工试播，不当作新的家庭检测通过。
云端尝试读取播放列表时执行环境代理不可连接，未连接到媒体源，不能据此判定源失效。
仅主订阅旧地址记录用户请求替换，未虚构卡顿反馈或新增good；精简版原地址另行保留作回退参考。
保留其他频道（包括刚发布的山西卫视）、发布hold和测试门槛。同步正式清单哈希。
后续需用户实际播放反馈；回退只处理河南卫视并重算哈希。

## 最新结论：本地试播失败，暂停处理

- 2026-10-03 23:38:57北京时间，用户反馈“河南卫视还是不行”。本轮PR #135换入的新签名链接实际试播失败，问题未解决；用户未描述黑屏、卡顿或错误码，不能进一步虚构具体症状或根因。
- 对应地址为本文的新地址（wsSecret=771e28a9b84e09e6e6ffd6bcaa6c2d7d）。历史QUALIFIED不能覆盖此次用户失败反馈；不得再称当前可用、已修好或本地验收通过。
- 23:43后补查20个日期的末份家庭报告，涵盖9月8日至10月3日。该抽查范围内，其他线路为REJECTED/BAD或UNKNOWN，未发现可直接替换的合格备用；不是完整历史库穷尽结论。UNKNOWN不等于已确认失败。
- 23:45:28用户决定“不管了记好文档”：暂停本次河南卫视换源处理，保留未解决状态。本次仅更新文档，不再换源、回退、触发检测或改调度。
- 后续维护先读本段。需新的不同线路可靠证据后再评估；不要继续把同一线路旧签名作为已验证修复方案让用户反复试。未进行新的自动检测或资格刷新。
- 本地失败反馈目前记录在本交接文档；本次未改机器反馈配置，接手自动选源前须同时核对本文。

## 本轮其他频道发布状态

- 河南卫视：[PR #135](https://github.com/pppaaasss/-/pull/135)已合并，发布提交33b8115e6ca4ba39e9972c4601d1127a1bec4108；发布成功不等于播放成功。
- 山西卫视：[PR #134](https://github.com/pppaaasss/-/pull/134)已发布，当前对话尚未收到本地验收反馈。
- 广东卫视：[PR #136](https://github.com/pppaaasss/-/pull/136)已发布，当前对话尚未收到本地验收反馈。
- 黑龙江卫视：未找到可靠替代，本轮未更换。
