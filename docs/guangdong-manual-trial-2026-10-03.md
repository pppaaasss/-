# 广东卫视换源交接（2026-10-03）
用户23:23:55北京时间再次要求替换广东卫视。上一轮写入被拒绝未发布，本次重新基于最新master，只换广东卫视。
新地址：http://120.40.39.246:352/newlive/live/hls/34/live.m3u8
四份订阅原地址：
- tv-core.m3u: http://hlsztemgsplive.miguvideo.com:8080/ws_w/2018/gdws/gdws711/1000/index.m3u8?msisdn=20260925020305299e02da4e764f9686d4037a66f3bc24&mdspid=&spid=699054&netType=0&sid=2202428795&pid=2028597139&timestamp=20260925020305&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=608831231&ParentNodeID=-99&assertID=2202428795&client_ip=171.8.79.254&SecurityKey=20260925020305&promotionId=&mvid=2202428795&mcid=500020&playurlVersion=WX-A1-9.9.2-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=8393796a454d8e5639e45b35faa2a7ed
- tv-easy.m3u: http://hlsztemgsplive.miguvideo.com:8080/ws_w/2018/gdws/gdws711/1000/index.m3u8?msisdn=20260925020305299e02da4e764f9686d4037a66f3bc24&mdspid=&spid=699054&netType=0&sid=2202428795&pid=2028597139&timestamp=20260925020305&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=608831231&ParentNodeID=-99&assertID=2202428795&client_ip=171.8.79.254&SecurityKey=20260925020305&promotionId=&mvid=2202428795&mcid=500020&playurlVersion=WX-A1-9.9.2-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=8393796a454d8e5639e45b35faa2a7ed
- tv.m3u: http://hlsztemgsplive.miguvideo.com:8080/ws_w/2018/gdws/gdws711/1000/index.m3u8?msisdn=20260925020305299e02da4e764f9686d4037a66f3bc24&mdspid=&spid=699054&netType=0&sid=2202428795&pid=2028597139&timestamp=20260925020305&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=608831231&ParentNodeID=-99&assertID=2202428795&client_ip=171.8.79.254&SecurityKey=20260925020305&promotionId=&mvid=2202428795&mcid=500020&playurlVersion=WX-A1-9.9.2-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=8393796a454d8e5639e45b35faa2a7ed
- tv-all.m3u: http://hlsztemgsplive.miguvideo.com:8080/ws_w/2018/gdws/gdws711/1000/index.m3u8?msisdn=20260925020305299e02da4e764f9686d4037a66f3bc24&mdspid=&spid=699054&netType=0&sid=2202428795&pid=2028597139&timestamp=20260925020305&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=608831231&ParentNodeID=-99&assertID=2202428795&client_ip=171.8.79.254&SecurityKey=20260925020305&promotionId=&mvid=2202428795&mcid=500020&playurlVersion=WX-A1-9.9.2-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=8393796a454d8e5639e45b35faa2a7ed
证据：home-reports/inbox/home-ac86u-8f8908f0fba9/20260928T183931Z-primary-0200-e584f0d1a427dc23.json。
candidate_id bed4aa15bf4dd5f58626b37e052d7ff9ea330d84131653b36123f141175d0a87。
2026-09-29 05:43:34北京时间，QUALIFIED，H264 1920×1080 25fps，下载12.519Mbps、码率9.098Mbps、余量1.376倍。
证据超过36小时有效期，按用户要求人工试播，当前播放效果待用户确认；未增加路由器测量、刷新资格或写good。
四份订阅各只换一条广东卫视媒体URL，其他条目保持不变，正式哈希绑定同步更新。
保留刚发布的河南、山西以及已确认的CCTV1、湖南等地址，保留自动发布hold。
旧源仅按用户要求替换记录，不虚构卡顿症状。刷新订阅后收集实际反馈。回退只恢复广东卫视并重算正式哈希。
