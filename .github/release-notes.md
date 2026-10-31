## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe。抖音功能无需 yt-dlp；仅使用 YouTube 直播资料时才需自行配置可信的 yt-dlp。浏览器登录与回退功能需要本机安装 Microsoft Edge。

## 本次更新

- 修复大目录的近期日常反复占满请求额度、导致历史列表一直无法继续的问题。新版给待续历史列表保留请求机会，并在不同扫描来源之间保留已完成进度。
- 多轮检查及重启后继续未完成扫描，每轮仍刷新最新内容；账号、登录状态或列表发生变化时重新验证，不会把未解决的分页问题误报为完成。
- 已经开始的作品流历史日常扫描，不再因为发现新的近期日常或其他来源的日常而被跳过。保留作者核对、去重、取消和登录失效处理。
- 修复 POSIX 环境迁移旧 Windows 路径时的分隔符问题，Windows 路径行为保持不变。
- 保留 v1.2.10 的超过 20 条日常分页、移动端身份查询、请求重试、Windows 路径修复和启动错误诊断。

本次更新来自 [#17](https://github.com/Ivan494/douyin-live-recorder/pull/17)。Windows 本地验证通过 526 个测试及 8 个子测试，另通过启动器的三种失败诊断检查；调度测试覆盖连续 100 轮及 JSON 状态重新加载，真实 40 条日常目录扫描也已回归通过。

发布构建固定使用 FFmpeg 8.1.2 essentials 包及其 SHA-256，并在解压前校验。包内附有 `release-manifest.json`，发布页面附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** 开启 Windows 智能应用控制的设备可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决。本次启动器改动仅帮助看清报错，不会绕过拦截。包内离线自检通过也不能证明智能应用控制放行；仍需在开启该功能的独立 Windows 设备上验证。如果仍被拦截，请在 issue 中提供应用版本、Windows 版本、启动器显示的错误代码和相关 CodeIntegrity 事件，并遮盖路径中的个人信息。

升级时请先保留旧安装目录，把新版解压到新目录。停止旧程序后再迁移自己的配置和录制文件；不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑后请重新登录。
