<p align="center">
  <a href="https://linux.do" title="LINUX DO">
    <img src="https://cdn3.ldstatic.com/original/4X/d/1/4/d146c68151340881c884d95e0da4acdf369258c6.png" alt="LINUX DO" width="100" height="100" />
  </a>
</p>

# 抖音直播录制

简体中文 | [English](#douyin-live-recorder)

本项目认可 [LINUX DO](https://linux.do) 社区。

Windows 便携版：录制抖音直播，并下载作品 / 日常。直播间探测走匿名请求（不带登录
Cookie），因此不会把你已经打开的抖音网页挤下线。

## 下载

到 [Releases](https://github.com/Ivan494/douyin-live-recorder/releases) 下载最新的
`DouyinLiveRecorder-vX.Y.Z-win64.zip`。解压到任意目录，运行
`DouyinLiveRecorder.exe`。已内置 Python 和 ffmpeg，不用再装别的。

默认**不会**开机自启。若需要，在设置里打开即可。
界面默认简体中文，可在设置里改成 English。

## 功能

- 直播录制成 MKV 分段，自动重连、卡顿检测
- 多账号并行，每个资料有独立的轮询间隔、清晰度和保存目录
- 作品和日常走移动端接口（X-Gorgon 签名）
- 纯 Python 签名（X-Bogus / Gorgon），盯直播不用开浏览器
- 可选导入 Chrome 登录态（本机 DPAPI 加密，不要提交到 git）
- 支持托盘的图形界面

## 从源码运行

需要：Windows、Python 3.11+，以及在 `PATH` 上的 `ffmpeg` / `ffprobe`
（或在 `src/settings.json` 里设置 `ffmpeg_path`）。

```text
pip install -r requirements.txt
python src/douyin_recorder_app.py
```

`src/DouyinLiveRecorder.exe` 是一个小启动器（没有捆绑运行时就会找 PATH 上的
`pythonw.exe` / `python.exe`）。

## 配置

| 文件 | 用途 |
| --- | --- |
| `src/profiles.json` | 监控的直播间 / 主页。默认是空列表。 |
| `src/settings.json` | 软件默认设置。开机自启默认关闭。 |
| `src/config.json` | 可选命令行值守脚本的模板。 |
| `src/douyin_session.json` | 本机加密登录态。已加入 .gitignore，不要提交。 |

路径占位符：

- `${DOWNLOAD_ROOT}` — `src/` 的上一级（可用 `--output-dir` 覆盖）
- `${TOOLS_DIR}` — 放 `ffmpeg.exe` 的目录

在界面里添加资料，或直接编辑 `src/profiles.json`。每个资料可单独设置输出目录、
轮询间隔、清晰度和是否自动下载。

### 日常

日常走移动端作品接口（`/aweme/v1/aweme/post/`），需要已保存的抖音登录态
（界面里的 **Session**）或资料里的 Cookie。勾选 **自动下载日常**，或：

```text
python src/douyin_media_downloader.py --profile-url <用户主页> --stories --output-dir <目录>
```

## 测试

```text
python -m pytest src/tests
```

## 打包 Windows 压缩包

```text
pip install -r requirements.txt pyinstaller
pwsh ./scripts/build_release.ps1 -Version 1.0.1 -FfmpegDir <含 ffmpeg.exe 的目录>
```

推送 `v*` 标签会在 GitHub Actions 上走同样的打包流程。

## 声明

仅供个人存档。请遵守抖音用户协议，尊重创作者权利。与抖音 / 字节跳动无关。

## 许可证

[MIT](LICENSE)

---

# Douyin Live Recorder

[简体中文](#抖音直播录制) | English

Acknowledges [LINUX DO](https://linux.do).

Portable Windows app that records Douyin live streams and downloads posted
works / stories. Live-room probes stay anonymous (no login cookies) so
monitoring does not kick an already-open Douyin session.

## Download

Grab the latest `DouyinLiveRecorder-vX.Y.Z-win64.zip` from
[Releases](https://github.com/Ivan494/douyin-live-recorder/releases).
Unzip anywhere and run `DouyinLiveRecorder.exe`. Python and ffmpeg are
bundled; nothing else to install.

The app does **not** start with Windows unless you turn that on in Settings.
The interface defaults to Simplified Chinese; switch to English in Settings.

## Features

- Live recording to MKV segments with auto-reconnect and stall detection
- Multiple profiles, each with its own interval, quality, and output folder
- Posted works and stories via the mobile API (X-Gorgon signed)
- Pure-Python request signing (X-Bogus / Gorgon) — no browser required for
  live monitoring
- Optional Chrome session import (DPAPI-encrypted, stored only on your machine)
- Tray-capable GUI

## Run from source

Requirements: Windows, Python 3.11+, and `ffmpeg` / `ffprobe` on `PATH`
(or set `ffmpeg_path` in `src/settings.json`).

```text
pip install -r requirements.txt
python src/douyin_recorder_app.py
```

`src/DouyinLiveRecorder.exe` is a tiny launcher that starts the GUI (falls
back to `pythonw.exe` / `python.exe` on PATH when no bundled runtime is
present).

## Configuration

| File | Purpose |
| --- | --- |
| `src/profiles.json` | Monitored channels. Empty by default. |
| `src/settings.json` | App defaults. Autostart is off. |
| `src/config.json` | Template for the optional CLI watcher. |
| `src/douyin_session.json` | Local encrypted session. Gitignored. Do not commit. |

Path tokens:

- `${DOWNLOAD_ROOT}` — parent of `src/` (override with `--output-dir`)
- `${TOOLS_DIR}` — folder that holds `ffmpeg.exe`

Add a profile in the GUI, or edit `src/profiles.json`. Each profile has its
own output directory, poll interval, quality, and download flags.

### Stories

Stories go through the mobile post API (`/aweme/v1/aweme/post/`). That path
needs a saved Douyin session (GUI **Session** dialog) or profile cookies.
Enable **Auto-download stories** per profile, or:

```text
python src/douyin_media_downloader.py --profile-url <user-url> --stories --output-dir <dir>
```

## Tests

```text
python -m pytest src/tests
```

## Build a Windows zip

```text
pip install -r requirements.txt pyinstaller
pwsh ./scripts/build_release.ps1 -Version 1.0.1 -FfmpegDir <folder-with-ffmpeg.exe>
```

Pushing a `v*` tag runs the same packaging on GitHub Actions.

## Disclaimer

For personal archival use only. Respect Douyin's terms of service and the
rights of content creators. Not affiliated with Douyin or ByteDance.

## License

[MIT](LICENSE)
