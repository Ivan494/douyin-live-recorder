## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe。抖音功能无需 yt-dlp；仅使用 YouTube 直播资料时才需自行配置可信的 yt-dlp。浏览器登录与回退功能需要本机安装 Microsoft Edge。

## 本次更新

- 根目录启动器在子程序启动失败时，会显示尝试启动的程序路径及可用的 Windows 错误或退出代码，以便排查 [#11](https://github.com/Ivan494/douyin-live-recorder/issues/11)。如果实际被拦截的是内部 DLL，这条消息不会直接指出它；还需查看 Windows CodeIntegrity 事件（如 3077）。
- 发布构建固定使用 FFmpeg 8.1.2 essentials 包及其 SHA-256，并在解压前校验。包内附有 `release-manifest.json`，发布页面附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** 开启 Windows 智能应用控制的设备可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决。本次启动器改动仅帮助看清报错，不会绕过拦截。包内离线自检通过也不能证明智能应用控制放行；仍需在开启该功能的独立 Windows 设备上验证。如果仍被拦截，请在 issue 中提供应用版本、Windows 版本、启动器显示的错误代码和相关 CodeIntegrity 事件，并遮盖路径中的个人信息。

升级时请先保留旧安装目录，把新版解压到新目录。停止旧程序后再迁移自己的配置和录制文件；不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑后请重新登录。
