## 下载与启动

下载本页的 `DouyinLiveRecorder-v@VERSION@-win64.zip`，完整解压后双击根目录的 `DouyinLiveRecorder.exe`。便携包内置 Python 运行时、FFmpeg 和 FFprobe。抖音功能无需 yt-dlp；YouTube 直播需自行配置可信的 yt-dlp。浏览器登录与回退功能需要本机安装 Microsoft Edge。

## 本次更新

- 补齐 v1.2.13 的回归审计发现：历史归档查询成功时，也必须查询并合并刷新视图，避免遗漏仅由刷新视图返回的日常。
- 两种历史视图以轮转方式共享每轮 15 次请求额度，分别保存游标及完成状态；一条未完成或失败的视图不会被另一条成功视图覆盖。
- 安全重新验证旧版无法确认所属视图的历史进度，保留已明确的归档进度、作品续扫游标和已下载文件记录。升级后的第一次历史扫描可能需要重新遍历；两种视图都有大量分页时，会在相同请求上限下分多轮完成。
- 保留超过 20 条日常的目录分页、作者核对、去重、重启续扫，以及 H.264 / HEVC 直播封装功能。

本次更新来自 [#21](https://github.com/Ivan494/douyin-live-recorder/pull/21)，继续处理 [#19](https://github.com/Ivan494/douyin-live-recorder/issues/19)。Windows 完整验证通过 591 个测试及 8 个子测试、三种启动器诊断检查。与 v1.2.9 / v1.2.12 / v1.2.13 的 24 种独立响应组合比较后，不再丢失此前可取得的条目；两次只读实测均取全 37 条目录。另有长分页、轮转预算、JSON 重载和三种旧状态格式的测试。问题报告人的具体主页仍待本人确认。

便携 EXE 离线自检现在要求额外的“仅刷新可见”条目也被保留（合成归档场景 4 条、刷新场景 2 条），并保留 H.264 / HEVC 实际封装及逐帧校验。发布构建使用经 SHA-256 校验的 FFmpeg 8.1.2；包内附有 `release-manifest.json`，本页附有 `SHA256SUMS.txt`。

**本压缩包未做 Windows 代码签名。** Windows 智能应用控制仍可能阻止启动；[#11](https://github.com/Ivan494/douyin-live-recorder/issues/11) 尚未解决。

请在直播结束、旧程序停止后升级。保留旧安装目录，把新版解压到新目录，迁移自己的配置和录制文件；不要用新版空白 `profiles.json`、`settings.json` 覆盖个人配置。登录态使用本机 Windows DPAPI 加密，换电脑后请重新登录。
