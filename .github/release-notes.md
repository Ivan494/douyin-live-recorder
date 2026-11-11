## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe，无需另装 Python。登录与浏览器回退功能需要本机安装 Microsoft Edge；YouTube 直播需要自行配置可信的 yt-dlp。

## 本次更新

- 修复程序完全退出后恢复录制任务时，清单中的缺失分段被静默移除的问题。现在保留完整分段顺序和编号，缺失已声明的分段时不发布缩短的录制，保留现存原始文件与清单供检查。
- 启动恢复中的临时文件访问错误不再导致遗漏分段或中断其他资料的恢复；所有分段都不存在的清单保持原样。
- 修复 FFmpeg 恰好在两次状态检查之间退出时，后续录制分段不再定时刷新直播地址的问题。启动下一段前先完成上一段的退出处理，保留日志、失败退避和待完成的封装操作，并重置每段的轮换状态。
- 修复直播源暂不可用、FFmpeg 未创建分段文件时，已有有效分段无法完成封装的问题。只移除本次明确未写入且不存在的分段路径，后续编号保持不变。
- 已观察到内容的分段后来消失或无法读取时，保留原始记录并报错，避免把缺失内容当作成功录制。重启后无法确认是否写入的分段也保守保留。
- 保留已有的严格与容错封装、断点发布恢复、历史日常双视图合并，以及 H.264 / HEVC 参数变化时的画面与音视频时间线。

包含此前 [#24](https://github.com/Ivan494/douyin-live-recorder/pull/24) 和 [#25](https://github.com/Ivan494/douyin-live-recorder/pull/25) 的录制修复。发布构建运行完整 Windows 回归测试、三种启动器诊断检查，并在中文及空格路径中解压后运行实际 EXE 的离线自检。H.264 / HEVC 合成素材逐帧验证各 16 帧；日常归档及刷新两种模式也进行合成检查。

发布构建使用经 SHA-256 校验的 FFmpeg 8.1.2。包内附有 `release-manifest.json`，本页附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** Windows 智能应用控制仍可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决。离线自检通过不代表该功能放行。历史日常问题 [#19](https://github.com/Ivan494/douyin-live-recorder/issues/19) 仍等待报告人确认其具体主页。

请在直播结束、旧程序停止后升级。保留旧安装目录，把新版解压到新目录，再迁移自己的配置、登录文件、录制文件及各输出目录中的 `douyin_media_state.json` 下载记录。不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑或 Windows 用户后请重新登录。
