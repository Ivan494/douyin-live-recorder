抖音直播录制 — Windows 64 位便携版

完整解压后，双击根目录的 DouyinLiveRecorder.exe。
已打包 Python 运行时、FFmpeg 和 FFprobe，无需另装 Python。
默认简体中文、空资料列表、关闭开机自启。请自行添加直播间或主页。
登录与浏览器回退功能需要本机安装 Microsoft Edge；YouTube 功能需要自行配置可信的 yt-dlp。

个人配置与登录信息保存在 douyindownload\_automation 下。
录制文件保存在 douyindownload 下。不要把这些个人文件上传到 GitHub。
登录态使用 Windows DPAPI 加密，不能当作可跨电脑使用的登录备份。

升级：先保留旧目录和录制文件，将新版解压到另一个新目录。
停止旧程序后，可复制自己的 profiles.json、settings.json 和录制文件。
不要直接覆盖正在录制的安装目录；不要把空白模板覆盖到自己的配置上。

压缩包附带 release-manifest.json；发布页面提供 SHA256SUMS.txt。
源码、更新说明与问题反馈：https://github.com/Ivan494/douyin-live-recorder
