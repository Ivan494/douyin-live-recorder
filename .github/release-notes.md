## 下载与启动

下载本页的 `DouyinLiveRecorder-v1.2.6-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。
已内置 Python 运行时、FFmpeg 和 FFprobe。默认简体中文、空资料列表、关闭开机自启。
浏览器登录与回退功能需要 Microsoft Edge；YouTube 功能需要另外配置 yt-dlp。

## 修复内容

- 修复朋友可见／互关可见的作品和日常能读取列表，却因媒体请求缺少登录 Cookie 而返回 403、下载失败的问题。视频、图文以及播放接口回退下载均使用有访问权限的登录账号。
- 资料单独设置的 Cookie 优先；未设置时使用已保存的抖音 App／网页登录。读取作品／日常列表与下载媒体使用同一账号，避免多个账号的登录态混用。
- 登录 Cookie 仅发送至 `douyin.com`、`iesdouyin.com`、`snssdk.com`、`amemv.com` 及其子域的 HTTPS 默认端口。每次重定向均重新检查；其他媒体 CDN、HTTP 降级、非默认端口和含身份信息的 URL 不携带登录 Cookie。
- 未登录时仍可下载公开作品；朋友可见内容需要当前账号本身具有访问权限。
- 感谢 [CNDY1390](https://github.com/CNDY1390) 提交 [#6](https://github.com/Ivan494/douyin-live-recorder/pull/6)，帮助定位日常媒体请求缺少认证的问题。

## 发布包验证

- 已通过 249 项自动化测试及 8 项子测试，覆盖作品／日常、视频／图文、播放接口回退、账号优先级及重定向认证边界。
- 已使用授权测试账号完成朋友可见视频的完整下载验证，并确认同一媒体地址不带 Cookie 时返回 403。
- 使用独立、版本锁定的依赖环境构建。
- 发布前检查源码、空白默认配置、文件白名单和文件哈希。
- 解压后通过根目录启动器执行 EXE 离线自检，包括中文/空格路径、Tk、签名依赖、FFmpeg 和 FFprobe。
- 附带 `SHA256SUMS.txt` 与包内 `release-manifest.json`。
- 发布包不包含个人监控列表、登录态、设备文件、日志、浏览器配置或录制文件。

## 升级说明

先保留旧安装目录，将新版解压到新目录。停止旧程序后再迁移自己的配置和录制文件。
不要把新版空白 `profiles.json` / `settings.json` 覆盖到正在使用的个人配置上。
登录态使用本机 Windows DPAPI 加密；换电脑后请重新登录。
