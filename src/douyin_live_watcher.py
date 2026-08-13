import argparse
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from streamget.platforms.douyin.live_stream import DouyinLiveStream

from recording_urls import has_recording_url, recording_extension, recording_input_url


STOP = False
APP_DIR = Path(__file__).resolve().parent
PACK_ROOT = APP_DIR.parent.parent
ROOT_DOWNLOAD_DIR = APP_DIR.parent
TOOLS_DIR = PACK_ROOT / "youtube-dl"


def handle_stop(_signum, _frame):
    global STOP
    STOP = True


def safe_name(value):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .")
    return cleaned or "douyin_live"


def expand_portable_path(value):
    if not isinstance(value, str):
        return value
    replacements = {
        "${PACK_ROOT}": PACK_ROOT,
        "%PACK_ROOT%": PACK_ROOT,
        "${DOWNLOAD_ROOT}": ROOT_DOWNLOAD_DIR,
        "%DOWNLOAD_ROOT%": ROOT_DOWNLOAD_DIR,
        "${TOOLS_DIR}": TOOLS_DIR,
        "%TOOLS_DIR%": TOOLS_DIR,
        r"E:\douyindownload": ROOT_DOWNLOAD_DIR,
        "E:/douyindownload": ROOT_DOWNLOAD_DIR,
        r"E:\youtube-dl": TOOLS_DIR,
        "E:/youtube-dl": TOOLS_DIR,
    }
    expanded = value
    for marker, path in replacements.items():
        expanded = expanded.replace(marker, str(path))
    return expanded


def expand_config_strings(data):
    if isinstance(data, dict):
        return {key: expand_config_strings(value) for key, value in data.items()}
    if isinstance(data, list):
        return [expand_config_strings(value) for value in data]
    return expand_portable_path(data)


def setup_logger(log_file):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("douyin-live-watcher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        config = expand_config_strings(json.load(fh))

    required = ["profile_url", "output_dir", "ffmpeg_path", "poll_interval_seconds", "container", "quality"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(missing)}")
    return config


def pid_is_running(pid):
    if not pid:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return str(pid) in result.stdout


def acquire_lock(lock_file, logger):
    if lock_file.exists():
        try:
            existing_pid = int(lock_file.read_text(encoding="utf-8").strip())
        except ValueError:
            existing_pid = None
        if pid_is_running(existing_pid):
            logger.info("Another watcher is already running with PID %s; exiting.", existing_pid)
            return False
        logger.info("Removing stale lock file.")
        lock_file.unlink(missing_ok=True)

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock(lock_file):
    try:
        if lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_file.unlink()
    except OSError:
        pass


async def resolve_stream(config, logger):
    # Anonymous room probes only: authenticated cookies cause Douyin multi-location kicks.
    live = DouyinLiveStream(
        proxy_addr=config.get("proxy_addr") or None,
        cookies=None,
        stream_orientation=config.get("stream_orientation", 1),
    )
    room = await live.fetch_app_stream_data(config["profile_url"])
    stream = await live.fetch_stream_url(room, config["quality"])

    logger.info(
        "Checked profile: anchor=%r status=%r live=%s live_url=%r title=%r",
        room.get("anchor_name"),
        room.get("status"),
        stream.is_live,
        room.get("live_url"),
        room.get("title"),
    )
    return room, stream


def run_ffmpeg(config, stream, logger):
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    title = safe_name(stream.title or "douyin_live")
    anchor = safe_name(stream.anchor_name or config.get("streamer_name") or "douyin")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    input_url, stream_kind = recording_input_url(stream)
    if not input_url:
        raise RuntimeError("Live stream did not include a recording URL")
    extension = recording_extension(input_url, config["container"])
    output_file = output_dir / f"{timestamp}_{anchor}_{title}.{extension}"

    from recording_urls import ffmpeg_live_input_options

    cmd = [
        config["ffmpeg_path"],
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-y",
        *ffmpeg_live_input_options(input_url),
        "-i",
        input_url,
        "-c",
        "copy",
        str(output_file),
    ]

    logger.info("Recording started: %s source=%s", output_file, stream_kind)
    process = subprocess.Popen(
        cmd,
        cwd=str(output_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    while process.poll() is None:
        if STOP:
            logger.info("Stop requested; terminating FFmpeg.")
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
            break
        time.sleep(5)

    stderr = ""
    if process.stderr:
        try:
            stderr = process.stderr.read()
        except OSError:
            stderr = ""

    logger.info("Recording ended: returncode=%s file=%s", process.returncode, output_file)
    if stderr.strip():
        logger.info("FFmpeg stderr: %s", stderr.strip()[-2000:])


async def watch(config, logger):
    poll_interval = int(config["poll_interval_seconds"])
    while not STOP:
        try:
            _room, stream = await resolve_stream(config, logger)
            if stream.is_live and has_recording_url(stream):
                run_ffmpeg(config, stream, logger)
            else:
                await asyncio.sleep(poll_interval)
        except Exception as exc:
            logger.exception("Watcher check failed: %s", exc)
            await asyncio.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="Resolve once and exit without recording.")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    config = load_config(args.config)
    log_file = Path(config.get("log_file") or Path(config["output_dir"]) / "logs" / "watcher.log")
    logger = setup_logger(log_file)
    lock_file = Path(config.get("lock_file") or Path(config["output_dir"]) / "watcher.lock")

    if not acquire_lock(lock_file, logger):
        return 0

    try:
        logger.info("Watcher starting with config: %s", args.config)
        if args.once:
            room, stream = asyncio.run(resolve_stream(config, logger))
            input_url, stream_kind = recording_input_url(stream)
            logger.info(
                "One-shot result: anchor=%r status=%r live=%s recording_url_present=%s source=%s",
                room.get("anchor_name"),
                room.get("status"),
                stream.is_live,
                bool(input_url),
                stream_kind or "none",
            )
            return 0
        asyncio.run(watch(config, logger))
        return 0
    finally:
        release_lock(lock_file)
        logger.info("Watcher stopped.")


if __name__ == "__main__":
    raise SystemExit(main())
