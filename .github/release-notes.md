## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe。抖音功能无需 yt-dlp；YouTube 直播需自行配置可信的 yt-dlp。浏览器登录与回退功能需要本机安装 Microsoft Edge。

## 本次更新

- 修复 PK / 连麦中途改变 H.264 序列头后，已录视频后半段无法正确解码的问题。直播先无损复制到 FLV 分段，结束后按参数变化整理并封装成一个 MKV。
- HEVC 在每个片段首个视频包内保留 VPS/SPS/PPS，修复仅切分文件仍可能无报错解出错误画面的问题。
- 保留同一次录制中的音视频时间线，包括 B 帧及参数切换处的真实间隔；避免重复 AAC 配置导致 MKV 头损坏。
- 仅保留首个视频和音频轨道，兼容旧 MKV 分段；最终封装失败时保留原始录制。已有录制文件不会自动重写。
- 保留 v1.2.11 的日常分页、可恢复扫描和 Windows 启动诊断。

本次更新来自 [#18](https://github.com/Ivan494/douyin-live-recorder/pull/18)。Windows 验证通过 547 个测试及 8 个子测试、三种启动器诊断检查。真实 FFmpeg 合成素材测试逐帧比较尺寸和像素校验值，并核对时间戳、多个输入轨道、截断尾部及失败保留行为。便携 EXE 离线自检也会运行 H.264 和 HEVC 的实际最终封装流程；这些测试使用合成 PK 切换素材，不代表已观测真实直播中的 PK 切换。

发布构建固定使用经 SHA-256 校验的 FFmpeg 8.1.2。包内附有 `release-manifest.json`，本页附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** Windows 智能应用控制仍可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决，离线自检不代表该功能放行。

请在直播结束、旧程序停止后升级。保留旧安装目录，把新版解压到新目录，迁移自己的配置和录制文件；不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑后请重新登录。
