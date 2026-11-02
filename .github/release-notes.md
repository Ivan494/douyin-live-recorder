## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe。抖音功能无需 yt-dlp；YouTube 直播需自行配置可信的 yt-dlp。浏览器登录与回退功能需要本机安装 Microsoft Edge。

## 本次更新

- 修复升级后只显示当前日常、漏掉历史归档的问题。恢复原来的作者历史请求，避免用刷新参数代替归档查询。
- 对原请求返回空白、未完成且没有有效游标的账号，保留刷新模式作为兼容回退。
- 两种请求模式分别保存游标及完成状态，安全迁移旧版进度；未完成的历史归档不会被一次成功刷新误报为完成。
- 保留大于 20 条日常的目录分页、作者核对、去重、重启续扫和每轮请求上限，以及 v1.2.12 的 H.264 / HEVC 直播封装修复。

本次更新来自 [#20](https://github.com/Ivan494/douyin-live-recorder/pull/20)，针对 [#19](https://github.com/Ivan494/douyin-live-recorder/issues/19)。Windows 验证通过 560 个测试及 8 个子测试、三种启动器诊断检查。参数敏感回放中，1 条当前日常及 2 条历史归档均可通过完整下载列表流程返回；已保存测试账号的两次只读扫描均取全 37 条目录。问题报告人的具体主页仍待本人验证。

便携 EXE 离线自检新增归档及刷新模式检查（合成数据），并保留 H.264 / HEVC 实际封装及逐帧校验。发布构建使用经 SHA-256 校验的 FFmpeg 8.1.2；包内附有 `release-manifest.json`，本页附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** Windows 智能应用控制仍可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决。

请在直播结束、旧程序停止后升级。保留旧安装目录，把新版解压到新目录，迁移自己的配置和录制文件；不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑后请重新登录。
