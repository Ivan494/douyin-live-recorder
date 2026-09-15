# 第三方组件

本软件打包 Python、Tk、PyInstaller 及 requirements-release.txt 中列出的 Python 依赖。
PyInstaller 会收集组件随包提供的许可证/元数据；各组件仍适用各自的许可证。

- Python：https://www.python.org/psf/license/
- PyInstaller（包含打包程序分发例外）：https://pyinstaller.org/en/stable/license.html
- Streamget：https://github.com/ihmily/streamget
- FFmpeg / FFprobe：https://ffmpeg.org/legal.html

FFmpeg Windows 二进制来自 https://www.gyan.dev/ffmpeg/builds/ 。对应的版本、构建配置和许可证可通过包内的 `ffmpeg.exe -version`、`ffmpeg.exe -buildconf`、`ffmpeg.exe -L` 查看；构建提供方页面包含源码与构建资料链接。请保留这些许可证说明。

项目源代码采用随包 LICENSE 中的 MIT 许可证。完整构建依赖版本记录在同版本源码的 requirements-release.txt 中。
