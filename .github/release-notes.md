## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe，无需另装 Python。登录与浏览器回退功能需要本机安装 Microsoft Edge；YouTube 直播需要自行配置可信的 yt-dlp。

## 本次更新

- 修复重连分段交界处时间戳轻微倒退导致最终封装反复失败的问题。仍先尝试严格无损合并，再按顺序尝试容错合并、分段整理及按顺序编号的多个 MKV。
- 发布前检查媒体轨道及完整时长，所有封装步骤共享总时限，并检查临时文件所需空间。失败或原始分段发生变化时保留原始录制，不把过短或不完整的结果当作成功。
- 最终文件发布中断后可从已验证的进度继续，不重复封装、不覆盖已有录制；Windows 发布不依赖硬链接，可用于不支持硬链接的移动存储。
- 命令行值守录制使用相同的最终封装流程，重启时恢复保存的录制任务；发现仍在写入的旧录制进程时，等待它结束后再处理，避免截断或重复录制。
- 保留历史归档与刷新视图合并、分页续扫，以及 H.264 / HEVC 参数变化时的画面和音视频时间线。

录制修复来自 [#23](https://github.com/Ivan494/douyin-live-recorder/pull/23)。发布构建运行完整回归测试、三种启动器诊断检查，并在中文及空格路径中解压后运行实际 EXE 的离线自检。H.264 / HEVC 合成素材逐帧验证各 16 帧；日常归档及刷新两种模式也通过合成检查。历史日常问题 [#19](https://github.com/Ivan494/douyin-live-recorder/issues/19) 仍等待报告人确认其具体主页。

发布构建使用经 SHA-256 校验的 FFmpeg 8.1.2。包内附有 `release-manifest.json`，本页附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** Windows 智能应用控制仍可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决。离线自检通过不代表该功能放行。

请在直播结束、旧程序停止后升级。保留旧安装目录，把新版解压到新目录，再迁移自己的配置、登录文件、录制文件及各输出目录中的 `douyin_media_state.json` 下载记录。不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑或 Windows 用户后请重新登录。
