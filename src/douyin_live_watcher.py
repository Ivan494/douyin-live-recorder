import argparse
import asyncio
import io
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import queue

from streamget.platforms.douyin.live_stream import DouyinLiveStream

from recording_segments import ffmpeg_live_capture_output_args
from recording_urls import (
    ffmpeg_live_input_options,
    has_recording_url,
    recording_extension,
    recording_input_url,
)
from security_utils import default_trusted_tool_roots, resolve_trusted_executable


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


def _hidden_process_kwargs():
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def _remux_capture(ffmpeg_path, capture_file, output_file, logger, stderr_file):
    """Use the recorder's validated, resumable publication before raw cleanup."""
    from douyin_recorder_app import MonitorEngine

    work_dir = capture_file.parent
    if work_dir.parent.name != ".recording_sessions":
        raise RuntimeError("Watcher captures must remain inside their recording session")
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "session.json"
    recording = MonitorEngine._read_recording_manifest(manifest_path)
    if recording is None:
        recording = {
            "profile_id": "watcher", "final_output": str(output_file),
            "session_dir": str(work_dir), "manifest_path": str(manifest_path),
            "parts": [str(capture_file)],
        }
    elif (Path(recording.get("final_output", "")) != output_file
          or recording.get("parts") != [str(capture_file)]):
        raise RuntimeError("Saved watcher publication belongs to a different capture")
    ffmpeg = Path(ffmpeg_path)
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    engine = MonitorEngine(SimpleNamespace(settings={
        "ffmpeg_path": str(ffmpeg), "ffprobe_path": str(ffprobe),
    }), queue.Queue())
    stderr_file.write(f"\n=== remux {capture_file.name} -> {output_file.name} ===\n")
    stderr_file.flush()
    engine._finalize_recording_worker(recording, "Watcher capture ended")
    if recording.get("status") != "complete":
        raise RuntimeError(recording.get("stop_reason") or "Recording finalization failed")
    logger.info("Recording remuxed: %s", recording.get("final_outputs") or [str(output_file)])


def _watcher_capture_processes():
    """Inspect writers; an unsuccessful scan must never imply an idle capture."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$ErrorActionPreference='Stop'; "
         "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
         "@(Get-CimInstance Win32_Process -Filter \"Name = 'ffmpeg.exe'\" | "
         "Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", timeout=10,
        **_hidden_process_kwargs(),
    )
    if result.returncode:
        raise RuntimeError("Could not establish whether saved captures are still running")
    processes = json.loads(result.stdout or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    if not isinstance(processes, list) or any(not p.get("CommandLine") for p in processes):
        raise RuntimeError("A capture process could not be inspected")
    return processes


def _watcher_session_has_writer(recording, processes):
    from douyin_recorder_app import pid_is_running as process_is_running

    if process_is_running(recording.get("capture_pid"),
                          not_started_after=recording.get("capture_started_at")):
        return True
    parts = {os.path.normcase(str(Path(part).resolve())) for part in recording["parts"]}
    for process in processes:
        arguments = shlex.split(process["CommandLine"], posix=False)
        if arguments and os.path.normcase(str(Path(arguments[-1].strip('"')).resolve())) in parts:
            return True
    return False


def recover_watcher_recordings(config, logger):
    """Resume saved watcher sessions before starting a fresh live capture."""
    output_dir = Path(config["output_dir"]).resolve()
    manifests = list((output_dir / ".recording_sessions").glob("*-watcher/session.json"))
    if not manifests:
        return True
    try:
        processes = _watcher_capture_processes()
    except Exception:
        logger.exception("Watcher recovery deferred until capture processes can be inspected")
        return False
    ffmpeg = resolve_trusted_executable(
        config["ffmpeg_path"], allowed_basenames={"ffmpeg.exe"},
        trusted_roots=default_trusted_tool_roots(APP_DIR, TOOLS_DIR),
    )
    ready = True
    for manifest in manifests:
        try:
            recording = json.loads(manifest.read_text(encoding="utf-8-sig"))
            output = Path(recording["final_output"])
            parts = [Path(part) for part in recording["parts"]]
            if (recording.get("profile_id") != "watcher" or not parts
                    or output.resolve().parent != output_dir
                    or any(part.resolve().parent != manifest.parent.resolve() for part in parts)):
                raise ValueError("Watcher session paths do not match its output folder")
            if _watcher_session_has_writer(recording, processes):
                logger.info("Waiting for the existing watcher capture to finish: %s", manifest.parent)
                ready = False
                continue
        except Exception:
            logger.exception("Watcher session could not be inspected; recovery deferred: %s", manifest)
            ready = False
            continue
        try:
            _remux_capture(ffmpeg, parts[0], output, logger, io.StringIO())
        except Exception:
            logger.exception("Watcher session could not be recovered; raw capture retained: %s", manifest)
    return ready


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
    # FLV keeps a mid-stream sequence header. Publish that file directly when
    # the configured container is FLV. Otherwise capture to FLV and remux to
    # MKV; a direct copy into Matroska would keep only the first header.
    if extension == "flv":
        capture_file = output_file
    else:
        if extension != "mkv":
            logger.info(
                "Container %s cannot preserve a mid-live sequence header; publishing MKV.",
                extension,
            )
            output_file = output_file.with_suffix(".mkv")
            extension = "mkv"
        session_dir = output_dir / ".recording_sessions" / (output_file.stem + "-watcher")
        session_dir.mkdir(parents=True, exist_ok=False)
        capture_file = session_dir / "part-0001.flv"
        (session_dir / "session.json").write_text(json.dumps({
            "profile_id": "watcher", "final_output": str(output_file),
            "session_dir": str(session_dir), "manifest_path": str(session_dir / "session.json"),
            "parts": [str(capture_file)], "status": "recording",
        }), encoding="utf-8")

    ffmpeg_path = resolve_trusted_executable(
        config["ffmpeg_path"],
        allowed_basenames={"ffmpeg.exe"},
        trusted_roots=default_trusted_tool_roots(APP_DIR, TOOLS_DIR),
    )
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-y",
        *ffmpeg_live_input_options(input_url),
        "-i",
        input_url,
        *ffmpeg_live_capture_output_args(capture_file),
    ]

    logger.info("Recording started: %s source=%s", capture_file, stream_kind)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = logs_dir / f"{output_file.stem}.ffmpeg.log"
    with open(stderr_path, "a", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            cmd,
            cwd=str(output_dir),
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            text=True,
            **_hidden_process_kwargs(),
        )
        if extension != "flv":
            try:
                manifest = session_dir / "session.json"
                recording = json.loads(manifest.read_text(encoding="utf-8"))
                recording.update(capture_pid=process.pid, capture_started_at=time.time())
                temporary = manifest.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(recording), encoding="utf-8")
                temporary.replace(manifest)
            except OSError:
                # Keep owning/waiting for the child even if metadata cannot be
                # saved. Recovery also discovers writers by their output path.
                logger.exception("Could not save capture identity; continuing to watch FFmpeg")
        while process.poll() is None:
            if STOP:
                logger.info("Stop requested; terminating FFmpeg.")
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                break
            time.sleep(0.5)
        logger.info(
            "Recording ended: returncode=%s file=%s log=%s",
            process.returncode,
            capture_file,
            stderr_path,
        )
        if extension == "flv" or not capture_file.exists() or capture_file.stat().st_size <= 0:
            return
        try:
            _remux_capture(ffmpeg_path, capture_file, output_file, logger, stderr_file)
        except Exception:
            logger.exception(
                "Could not remux %s; playable FLV capture kept at %s",
                output_file,
                capture_file,
            )


async def watch(config, logger):
    poll_interval = int(config["poll_interval_seconds"])
    while not STOP and not recover_watcher_recordings(config, logger):
        await asyncio.sleep(poll_interval)
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
