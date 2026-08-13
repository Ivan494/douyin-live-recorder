# Douyin Live Recorder

Portable Windows app that records Douyin live streams and downloads posted
works / stories. Live-room probes stay anonymous (no login cookies) so
monitoring does not kick an already-open Douyin session.

## Download

Grab the latest `DouyinLiveRecorder-vX.Y.Z-win64.zip` from
[Releases](https://github.com/Ivan494/douyin-live-recorder/releases).
Unzip anywhere and run `DouyinLiveRecorder.exe`. Python and ffmpeg are
bundled; nothing else to install.

The app does **not** start with Windows unless you turn that on in Settings.

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
