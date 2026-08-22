独立 Windows 便携包。解压后运行 DouyinLiveRecorder.exe。已内置 ffmpeg，无需安装 Python。

界面默认简体中文，可在设置里改成 English。

本版更新（安全与稳定性）：
- 登录态不再写入 profiles.json，仅保存在本机 DPAPI 加密文件中
- 新增「清除已保存登录」，退出时同步清理浏览器 Cookie 与设备绑定
- FFmpeg 仅接受安全的 http(s) 直播地址，拒绝 file/内网/本地回环等协议
- 作品与日常下载限制在抖音相关 CDN 域名，分享短链逐跳校验
- 登录浏览器在导入成功后自动关闭；CDP 仅绑定本机回环地址
- 设置中的 ffmpeg / yt-dlp 路径需位于可信目录或 PATH
- Release 构建会校验 ffmpeg 压缩包 SHA-256
- 补充 pycryptodome 依赖，修复移动端签名在部分环境下的静默失败
