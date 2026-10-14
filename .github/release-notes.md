## 下载与启动

下载本页的 `DouyinLiveRecorder-v1.2.9-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe；抖音录制仍需要 FFmpeg，无需 yt-dlp。仅使用 YouTube 直播资料时才需自行配置可信的 yt-dlp，便携包不包含它。

## 本次修复

- 修复未安装 yt-dlp 时，抖音用户无法保存设置的问题。现在可以先保存位于可信工具目录的 yt-dlp 路径；实际使用 YouTube 功能时仍会检查该程序是否存在且可信。
- 设置界面明确标注 yt-dlp 仅供 YouTube 使用。

[#7](https://github.com/Ivan494/douyin-live-recorder/issues/7) 中关于历史日常能否完整获取的问题仍需可验证的样例确认；本次更新只修复上述设置保存问题。

发布流程会检查源码和空白默认配置，运行回归测试，并在发布前解压安装包执行离线自检。压缩包附带 `release-manifest.json`，发布页面附带 `SHA256SUMS.txt`。

升级时请先保留旧安装目录，把新版解压到新目录。停止旧程序后再迁移自己的配置和录制文件；不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑后请重新登录。
