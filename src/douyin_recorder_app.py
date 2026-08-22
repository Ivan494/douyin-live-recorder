import argparse
import asyncio
import base64
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, Toplevel, messagebox, filedialog
from tkinter import ttk
from types import SimpleNamespace
from urllib.parse import urlparse

import pystray
from PIL import Image, ImageDraw
from streamget.platforms.douyin.live_stream import DouyinLiveStream

from douyin_media_downloader import (
    CaptchaDetectedError,
    DEFAULT_CHROME_CDP,
    clear_saved_session,
    close_cdp_browser,
    download_profile,
    download_video_by_url,
    import_chrome_session,
    launch_douyin_login_browser,
    saved_session_info,
)
from recording_urls import (
    ffmpeg_live_input_options,
    has_recording_url,
    is_safe_recording_url,
    recording_input_url,
)
from security_utils import default_trusted_tool_roots, resolve_trusted_executable
from i18n import LANGUAGE_CHOICES, set_language, t


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PACK_ROOT = APP_DIR.parent.parent
ROOT_DOWNLOAD_DIR = APP_DIR.parent
TOOLS_DIR = PACK_ROOT / "youtube-dl"
DEFAULT_FFMPEG_PATH = TOOLS_DIR / "ffmpeg.exe"
DEFAULT_FFPROBE_PATH = TOOLS_DIR / "ffprobe.exe"
DEFAULT_YTDLP_PATH = TOOLS_DIR / "yt-dlp.exe"
PROFILES_FILE = APP_DIR / "profiles.json"
SETTINGS_FILE = APP_DIR / "settings.json"
# FIX-AUDIT-5: Persist circuit breaker state across restarts
MEDIA_CIRCUIT_BREAKER_FILE = APP_DIR / "media_circuit_breaker.json"
APP_LOG_FILE = APP_DIR / "logs" / "app.log"
APP_LOCK_FILE = APP_DIR / "douyin_recorder_app.lock"
DEFAULT_NEW_PROFILE_INTERVAL = 60
DEFAULT_MEDIA_INTERVAL = 300
SHOW_SIGNAL_FILE = APP_DIR / "show_window.signal"
STARTUP_ENTRY_NAME = "Douyin Recorder App.lnk"
QUALITY_OPTIONS = ("OD", "UHD", "HD", "SD", "LD")
POWERSHELL_TIMEOUT_SECONDS = 15
PROFILE_TABLE_COLUMNS = (
    "enabled",
    "name",
    "status",
    "media_auto",
    "media_progress",
    "next_check",
)


def wants_live_recording(profile):
    """Probe and record live streams only when the profile and live toggle are on."""
    if not isinstance(profile, dict):
        return False
    return bool(profile.get("enabled", True)) and bool(profile.get("record_live", True))


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_entry_path():
    directory = startup_dir()
    return directory / STARTUP_ENTRY_NAME if directory else None


def startup_launcher():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve(), "", APP_DIR

    exe_launcher = APP_DIR / "DouyinLiveRecorder.exe"
    if exe_launcher.exists():
        return exe_launcher, "", APP_DIR

    raise FileNotFoundError(
        f"Autostart requires the UI launcher EXE and it was not found: {exe_launcher}"
    )


def powershell_single_quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_powershell_script(script, timeout=POWERSHELL_TIMEOUT_SECONDS):
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_script,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        **hidden_subprocess_kwargs(),
    )
    if result.returncode != 0:
        details = "\n".join(
            part.strip() for part in (result.stderr, result.stdout) if part and part.strip()
        )
        raise RuntimeError(
            "PowerShell command failed: "
            + (details or f"exit code {result.returncode}")
        )
    return result.stdout.strip()


def write_startup_shortcut(path):
    target, arguments, working_directory = startup_launcher()
    script = f"""
$ErrorActionPreference = 'Stop'
$shortcutPath = {powershell_single_quoted(path)}
$targetPath = {powershell_single_quoted(target)}
$shortcutArguments = {powershell_single_quoted(arguments)}
$workingDirectory = {powershell_single_quoted(working_directory)}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.Arguments = $shortcutArguments
$shortcut.WorkingDirectory = $workingDirectory
$shortcut.Save()
"""
    try:
        run_powershell_script(script)
    except RuntimeError as exc:
        raise RuntimeError(f"PowerShell shortcut creation failed: {exc}") from exc


def startup_shortcut_details(path):
    if not path or not path.exists():
        return None
    script = f"""
$ErrorActionPreference = 'Stop'
$shortcutPath = {powershell_single_quoted(path)}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
[PSCustomObject]@{{
    TargetPath = $shortcut.TargetPath
    Arguments = $shortcut.Arguments
    WorkingDirectory = $shortcut.WorkingDirectory
}} | ConvertTo-Json -Compress
"""
    return json.loads(run_powershell_script(script))


def same_path(left, right):
    left_text = os.path.abspath(str(left))
    right_text = os.path.abspath(str(right))
    if os.name == "nt":
        return os.path.normcase(left_text) == os.path.normcase(right_text)
    return left_text == right_text


def startup_shortcut_matches():
    path = startup_entry_path()
    details = startup_shortcut_details(path)
    if not details:
        return False
    target, arguments, working_directory = startup_launcher()
    return (
        same_path(details.get("TargetPath", ""), target)
        and details.get("Arguments", "") == arguments
        and same_path(details.get("WorkingDirectory", ""), working_directory)
    )


def remove_startup_entries():
    path = startup_entry_path()
    if path:
        path.unlink(missing_ok=True)
        for legacy_path in path.parent.glob("Douyin Recorder App*.vbs"):
            legacy_path.unlink(missing_ok=True)


def is_autostart_enabled():
    try:
        return startup_shortcut_matches()
    except Exception:
        logging.exception("Could not inspect Startup shortcut")
        return False


def set_autostart_enabled(enabled):
    path = startup_entry_path()
    if not path:
        raise OSError("APPDATA is not available.")
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_startup_shortcut(path)
    else:
        remove_startup_entries()


def safe_name(value):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value)).strip(" .")
    return cleaned or "live_recording"


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


def portableize_path(value):
    if not isinstance(value, str):
        return value

    def replace_prefix(text, root, marker):
        root_text = str(root)
        if os.name == "nt":
            matches = text.casefold().startswith(root_text.casefold())
        else:
            matches = text.startswith(root_text)
        if not matches:
            return text
        return marker + text[len(root_text):]

    text = value
    for root, marker in (
        (TOOLS_DIR, "${TOOLS_DIR}"),
        (ROOT_DOWNLOAD_DIR, "${DOWNLOAD_ROOT}"),
        (PACK_ROOT, "${PACK_ROOT}"),
    ):
        changed = replace_prefix(text, root, marker)
        if changed != text:
            return changed
    return text


def map_config_strings(data, mapper):
    if isinstance(data, dict):
        return {key: map_config_strings(value, mapper) for key, value in data.items()}
    if isinstance(data, list):
        return [map_config_strings(value, mapper) for value in data]
    if isinstance(data, str):
        return mapper(data)
    return data


def detect_platform(url):
    lowered = (url or "").lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if "douyin.com" in lowered:
        return "douyin"
    return "unknown"


def platform_label(platform):
    return {"douyin": t("platform_douyin"), "youtube": t("platform_youtube")}.get(platform, t("platform_unknown"))


def fallback_name_from_url(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    slug = parts[-1] if parts else parsed.netloc or "profile"
    slug = slug.lstrip("@") or "profile"
    return f"{platform_label(detect_platform(url))} {safe_name(slug)}"


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seconds_text(seconds):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        seconds = 0
    return str(timedelta(seconds=seconds))


def future_text(seconds):
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")


def print_console(text):
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write((str(text) + "\n").encode(encoding, errors="backslashreplace"))


def file_size_text(path):
    if not path:
        return ""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return ""
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
    return f"{size / (1024 * 1024):.1f} MB"


def byte_size_text(size):
    try:
        size = max(0, int(size or 0))
    except (TypeError, ValueError):
        size = 0
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def media_progress_text(progress):
    phase = progress.get("phase") or ""
    if phase == "resolving":
        return t("progress_resolving")
    if phase == "scanning":
        pages = int(progress.get("pages") or 0)
        found = int(progress.get("found") or 0)
        if not pages:
            return t("progress_scanning")
        return t("progress_history", pages=pages, found=found)
    if phase == "checking_stories":
        return t("progress_stories")

    is_story = progress.get("media_kind") == "story"
    media_kind = t("kind_story") if is_story else t("kind_video")
    current = int(progress.get("current") or 0)
    total = int(progress.get("total") or 0)
    downloaded = int(progress.get("downloaded") or 0)
    skipped = int(progress.get("skipped") or 0)
    failed = int(progress.get("failed") or 0)
    item = str(progress.get("item") or "").strip()
    if len(item) > 34:
        item = item[:33] + "…"

    if not current:
        kind = t("kind_story_lower") if is_story else t("kind_video_lower")
        return t("progress_preparing", total=total, kind=kind)

    prefix = f"{media_kind} {current}/{total}"
    if phase == "retrying":
        attempt = int(progress.get("attempt") or 1)
        attempts = int(progress.get("attempts") or attempt)
        text = t("progress_retry", prefix=prefix, attempt=attempt, attempts=attempts)
    elif "bytes_downloaded" in progress:
        transferred = int(progress.get("bytes_downloaded") or 0)
        expected = int(progress.get("bytes_total") or 0)
        if expected:
            text = f"{prefix} • {byte_size_text(transferred)} / {byte_size_text(expected)}"
        elif transferred:
            text = f"{prefix} • {byte_size_text(transferred)}"
        else:
            text = t("progress_connecting", prefix=prefix)
    else:
        text = t("progress_counts", prefix=prefix, downloaded=downloaded, skipped=skipped, failed=failed)
    if item:
        text += f" • {item}"
    return text


def last_log_line(path):
    if not path:
        return ""
    try:
        log_path = Path(path)
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            data = fh.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            return redact_sensitive_text(stripped)[:180]
    return ""


def redact_sensitive_text(text):
    """Strip query strings from URLs so signed CDN tokens do not reach the UI."""
    return re.sub(r"(https?://[^\s\"']+?)(\?[^\s\"']*)", r"\1?[redacted]", str(text or ""))


def _looks_like_cookie_header(value):
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(name in lowered for name in ("sessionid=", "uid_tt=", "sid_tt=", "sid_guard=")):
        return True
    if ";" in text and "=" in text and not Path(text).suffix:
        return True
    return False


def _trusted_tool_roots():
    return default_trusted_tool_roots(APP_DIR, TOOLS_DIR)


def resolve_ffmpeg_executable(path_text):
    return resolve_trusted_executable(
        path_text,
        allowed_basenames={"ffmpeg.exe"},
        trusted_roots=_trusted_tool_roots(),
    )


def resolve_ytdlp_executable(path_text):
    return resolve_trusted_executable(
        path_text,
        allowed_basenames={"yt-dlp.exe"},
        trusted_roots=_trusted_tool_roots(),
    )


def hidden_subprocess_kwargs():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def setup_logging(*, console=False):
    APP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers = [
        RotatingFileHandler(APP_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
    ]
    if console and sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)


def install_exception_hooks(root=None):
    def log_unhandled(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    def log_thread_exception(args):
        logging.critical(
            "Unhandled thread exception in %s",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_unhandled
    if hasattr(threading, "excepthook"):
        threading.excepthook = log_thread_exception

    if root is not None:
        def report_callback_exception(exc_type, exc_value, exc_traceback):
            logging.error("Unhandled GUI callback exception", exc_info=(exc_type, exc_value, exc_traceback))
            try:
                messagebox.showerror(t("unexpected_error"), f"{exc_type.__name__}: {exc_value}")
            except Exception:
                pass

        root.report_callback_exception = report_callback_exception


def _windows_process_state(pid):
    """Return (is_running, creation_time) for a Windows PID.

    creation_time is the raw 100-nanosecond FILETIME value. It lets the lock
    check distinguish the original recorder process from an unrelated process
    that later reused the same PID.
    """
    process_query_limited_information = 0x1000
    still_active = 259

    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    open_process.restype = ctypes.c_void_p
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    get_exit_code.restype = ctypes.c_int
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    )
    get_process_times.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        # Access denied still means the process exists, but its identity cannot
        # be checked. Invalid PIDs return false.
        return ctypes.get_last_error() == 5, None
    try:
        exit_code = ctypes.c_ulong()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return True, None
        if exit_code.value != still_active:
            return False, None

        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return True, None
        creation_time = (int(creation.high) << 32) | int(creation.low)
        return True, creation_time
    finally:
        close_handle(handle)


def pid_is_running(pid, *, not_started_after=None):
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    running, creation_time = _windows_process_state(pid)
    if not running or not_started_after is None or creation_time is None:
        return running

    # Windows FILETIME is measured in 100 ns ticks since 1601-01-01 UTC.
    windows_to_unix_epoch = 116444736000000000
    creation_timestamp = (creation_time - windows_to_unix_epoch) / 10_000_000
    return creation_timestamp <= float(not_started_after)

def _read_lock_pid():
    """Return PID from lock file (plain integer or small JSON)."""
    try:
        raw = APP_LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            return int(payload.get("pid"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    try:
        return int(raw.splitlines()[0].strip())
    except ValueError:
        return None


def _write_lock_pid(pid):
    payload = {
        "pid": int(pid),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    APP_LOCK_FILE.write_text(json.dumps(payload), encoding="utf-8")


def request_running_instance_show(existing_pid=None):
    """Ask the live instance to show its window (signal file + Win32 restore)."""
    try:
        SHOW_SIGNAL_FILE.write_text(str(datetime.now().timestamp()), encoding="utf-8")
    except OSError:
        logging.exception("Could not write show-window signal")
    if existing_pid:
        restore_window_for_pid(existing_pid)


def restore_window_for_pid(pid):
    """Best-effort restore of a Tk main window owned by pid (Windows only)."""
    if os.name != "nt" or not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    GetWindowTextW = user32.GetWindowTextW
    IsIconic = user32.IsIconic
    ShowWindow = user32.ShowWindow
    SetForegroundWindow = user32.SetForegroundWindow
    BringWindowToTop = user32.BringWindowToTop
    MoveWindow = user32.MoveWindow
    SetWindowPos = user32.SetWindowPos
    GetWindowRect = user32.GetWindowRect

    class RECT(ctypes.Structure):
        _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long))

    found = []

    def callback(hwnd, _lparam):
        window_pid = ctypes.c_ulong()
        GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) != pid:
            return True
        length = GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value or ""
        if "Douyin Live Recorder" in title or "抖音直播录制" in title:
            found.append(hwnd)
            return False
        if "Douyin" in title and "Recorder" in title:
            found.append(hwnd)
        return True

    EnumWindows(EnumWindowsProc(callback), 0)
    if not found:
        return False

    hwnd = found[0]
    SW_RESTORE = 9
    SW_SHOW = 5
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_SHOWWINDOW = 0x0040

    try:
        if IsIconic(hwnd):
            ShowWindow(hwnd, SW_RESTORE)
        ShowWindow(hwnd, SW_SHOW)
        # Keep on primary-ish coordinates if the window is far off-screen.
        rect = RECT()
        if GetWindowRect(hwnd, ctypes.byref(rect)):
            width = max(800, rect.right - rect.left)
            height = max(500, rect.bottom - rect.top)
            if rect.left < -100 or rect.top < -100 or rect.left > 4000 or rect.top > 3000:
                MoveWindow(hwnd, 120, 60, min(width, 1280), min(height, 800), True)
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_SHOWWINDOW)
        BringWindowToTop(hwnd)
        SetForegroundWindow(hwnd)
        SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_SHOWWINDOW)
        SetForegroundWindow(hwnd)
        logging.info("Restored existing recorder window for PID %s", pid)
        return True
    except Exception:
        logging.exception("Could not restore window for PID %s", pid)
        return False


def acquire_app_lock():
    while True:
        try:
            fd = os.open(APP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                payload = {
                    "pid": os.getpid(),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
                fh.write(json.dumps(payload))
            return True
        except FileExistsError:
            pass
        except OSError as exc:
            logging.warning("Could not create app lock: %s", exc)
            return True

        existing_pid = _read_lock_pid()
        try:
            # A live process whose creation time is newer than the lock file is
            # a PID-reuse collision, not the recorder that created this lock.
            lock_identity_deadline = APP_LOCK_FILE.stat().st_mtime + 2
        except OSError:
            lock_identity_deadline = None

        if pid_is_running(existing_pid, not_started_after=lock_identity_deadline):
            logging.info("Douyin Live Recorder is already running with PID %s; requesting show.", existing_pid)
            request_running_instance_show(existing_pid)
            return False

        logging.info("Removing stale app lock (pid=%s not running).", existing_pid)
        APP_LOCK_FILE.unlink(missing_ok=True)


def release_app_lock():
    try:
        if not APP_LOCK_FILE.exists():
            return
        existing_pid = _read_lock_pid()
        if existing_pid == os.getpid():
            APP_LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def default_settings():
    return {
        "poll_interval_seconds": 60,
        "new_profile_poll_interval_seconds": DEFAULT_NEW_PROFILE_INTERVAL,
        "ffmpeg_path": str(DEFAULT_FFMPEG_PATH),
        "ytdlp_path": str(DEFAULT_YTDLP_PATH),
        "container": "mkv",
        "quality": "OD",
        "language": "zh-CN",
        "start_hidden_to_tray": False,
        "priority_risk_control_backoff_seconds": 60,
        "standard_risk_control_backoff_seconds": 60,
        "priority_poll_jitter_seconds": 2,
        "poll_jitter_seconds": 8,
        "media_poll_interval_seconds": DEFAULT_MEDIA_INTERVAL,
        "adopt_existing_ffmpeg": True,
        "recording_stall_timeout_seconds": 300,
        "recording_offline_grace_seconds": 90,
        "recording_segment_max_seconds": 2700,
        "recording_reconnect_delay_max_seconds": 5,
        "recording_engine_version": 2,
        "not_visible_backoff_seconds": 300,
        "unsupported_stream_backoff_seconds": 300,
        "captcha_backoff_seconds": 120,  # FIX-CB: reduced from 600s; escalates via consecutive_failures
        "error_min_backoff_seconds": 30,
        "error_max_backoff_seconds": 300,
        # Keep defaults side-effect free. RecorderStore performs the one-time
        # shortcut check only when migrating or creating a settings file.
        "start_with_windows": False,
    }


def default_profiles():
    return []


def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return map_config_strings(json.load(fh), expand_portable_path)
    except OSError as exc:
        # FIX-H4: File deleted/locked between exists() and open() (antivirus, disk unmount).
        logging.warning("Could not read config file %s: %s", path, exc)
        return fallback
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        # FIX-T2: Quarantine corrupt file and fall back to defaults instead of crashing.
        corrupt_name = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        logging.error("Config file %s is corrupt (%s); quarantining to %s", path, exc, corrupt_name.name)
        try:
            path.rename(corrupt_name)
        except OSError:
            pass
        return fallback


CONFIG_BACKUP_KEEP = 20


def _backup_config_file(path):
    # FIX-BACKUP: Keep rotating timestamped backups of the previous config
    # file before every save, so a bad save (e.g. a stray app instance
    # persisting an empty profile list) can never destroy the only copy.
    try:
        if not path.exists() or path.stat().st_size == 0:
            return
        backup_dir = path.parent / "config_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{path.name}.{stamp}.{os.getpid()}.bak"
        shutil.copy2(path, backup)
        existing = sorted(
            backup_dir.glob(path.name + ".*.bak"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in existing[:-CONFIG_BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        logging.exception("Could not back up config file %s", path)


def save_json(path, data):
    # FIX-M10: Wrap in try/except to prevent disk-full or locked-file errors
    # from killing the monitor thread silently.
    # FIX-APP-1: Use unique tmp filename to prevent concurrent-save corruption.
    # FIX-BACKUP: snapshot the previous file first (rotating, kept in config_backups/).
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup_config_file(path)
        if isinstance(data, list) and not data and path.exists() and path.stat().st_size > 5:
            # FIX-GUARD: hard refusal to wipe a non-empty config with an empty
            # list. Two real incidents (2026-08-04) proved a stray process can
            # hold an empty in-memory list and persist it over the good file.
            # The previous file was already snapshotted above; keep it as-is.
            logging.error(
                "REFUSED to overwrite %s with an EMPTY list; existing file kept "
                "(snapshot also in config_backups/). To remove the last entry, "
                "edit the file manually while the app is closed.",
                path,
            )
            return
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(map_config_strings(data, portableize_path), fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError:
        logging.exception("Could not save config file %s (disk full or locked?)", path)

class RecorderStore:
    def __init__(self):
        self.settings = load_json(SETTINGS_FILE, default_settings())
        set_language(self.settings.get("language"))
        if "start_with_windows" not in self.settings:
            self.settings["start_with_windows"] = is_autostart_enabled()
        # Repair the startup shortcut when the setting is enabled but the
        # shortcut is missing or broken (e.g. after a directory move or a
        # failed PowerShell read).  Failures here are non-fatal — the app can
        # still run; the user will see the toggle state in Settings.
        if self.settings.get("start_with_windows") and not is_autostart_enabled():
            try:
                set_autostart_enabled(True)
            except Exception:
                logging.exception("Could not repair autostart shortcut on startup")
        self.profiles = load_json(PROFILES_FILE, default_profiles())
        if isinstance(self.profiles, dict):
            self.profiles = [self.profiles]
        if (
            isinstance(self.profiles, list)
            and len(self.profiles) == 1
            and isinstance(self.profiles[0], dict)
            and isinstance(self.profiles[0].get("value"), list)
        ):
            self.profiles = self.profiles[0]["value"]
        if self.normalize():
            self.save()

    def normalize(self):
        migrated = False
        defaults = default_settings()
        for key, value in defaults.items():
            if key not in self.settings:
                self.settings[key] = value
                migrated = True
        try:
            recording_engine_version = int(self.settings.get("recording_engine_version") or 0)
        except (TypeError, ValueError):
            recording_engine_version = 0
        if recording_engine_version < 2:
            self.settings["container"] = "mkv"
            self.settings["recording_engine_version"] = 2
            for profile in self.profiles:
                if isinstance(profile, dict):
                    profile["container"] = "mkv"
            migrated = True
        if str(self.settings.get("container") or "").lower() != "mkv":
            self.settings["container"] = "mkv"
            migrated = True
        if str(self.settings.get("quality", "OD")).upper() not in QUALITY_OPTIONS:
            self.settings["quality"] = "OD"
            migrated = True

        self.profiles = [
            profile for profile in self.profiles
            if profile.get("url", "").strip() or profile.get("name") != "Live Profile"
        ]
        for profile in self.profiles:
            existing_interval = self.settings.get("poll_interval_seconds", DEFAULT_NEW_PROFILE_INTERVAL)
            profile.setdefault("id", str(uuid.uuid4()))
            profile.setdefault("enabled", True)
            profile.setdefault("record_live", True)
            profile.setdefault("name", "Live Profile")
            profile.setdefault("url", "")
            profile.setdefault("platform", detect_platform(profile.get("url", "")))
            profile.setdefault("output_dir", str(ROOT_DOWNLOAD_DIR / safe_name(profile["name"])))
            profile.setdefault("quality", self.settings["quality"])
            if str(profile.get("quality", "OD")).upper() not in QUALITY_OPTIONS:
                profile["quality"] = "OD"
                migrated = True
            if str(profile.get("container") or "").lower() != "mkv":
                profile["container"] = "mkv"
                migrated = True
            profile.setdefault("cookies", "")
            # Never keep Douyin session cookie headers in profiles.json.
            # Auth lives in DPAPI-encrypted session files only. YouTube may
            # still use cookies as a Netscape cookie *file path*.
            cookies_value = str(profile.get("cookies") or "").strip()
            if cookies_value and _looks_like_cookie_header(cookies_value):
                profile["cookies"] = ""
                migrated = True
            profile.setdefault("proxy_addr", "")
            profile.setdefault("stream_orientation", 1)
            profile.setdefault("poll_interval_seconds", existing_interval)
            profile.setdefault("priority", False)
            profile.setdefault("fallback_live_url", "")
            profile.setdefault("original_profile_url", "")
            profile.setdefault("auto_download_videos", False)
            profile.setdefault("auto_download_stories", False)
            profile.setdefault("media_poll_interval_seconds", self.settings.get("media_poll_interval_seconds", DEFAULT_MEDIA_INTERVAL))
            profile["platform"] = detect_platform(profile.get("url", "")) if profile.get("platform") in ("", "unknown", None) else profile["platform"]
        return migrated

    def save(self):
        save_json(SETTINGS_FILE, self.settings)
        save_json(PROFILES_FILE, self.profiles)

    def save_profiles_only(self):
        """FIX-APP-9: Save only profiles (skip settings) for monitor-thread updates."""
        save_json(PROFILES_FILE, self.profiles)

    def get_profile(self, profile_id):
        for profile in self.profiles:
            if profile["id"] == profile_id:
                return profile
        return None

    def upsert_profile(self, profile):
        if not profile.get("id"):
            profile["id"] = str(uuid.uuid4())
        existing = self.get_profile(profile["id"])
        if existing:
            existing.update(profile)
        else:
            self.profiles.append(profile)
        self.normalize()
        self.save()

    def remove_profile(self, profile_id):
        self.profiles = [p for p in self.profiles if p["id"] != profile_id]
        self.save()


class MonitorEngine:
    def __init__(self, store, event_queue):
        self.store = store
        self.event_queue = event_queue
        self.thread = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.processes = {}
        self.stderr_files = {}
        self.recordings = {}
        self.state = {}
        self.next_check = {}
        self.error_counts = {}
        self.last_error_kinds = {}
        self.finalizer_threads = []

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.is_running():
            return
        self.stop_event.clear()
        self.wake_event.clear()
        self.next_check.clear()
        if self.store.settings.get("adopt_existing_ffmpeg", True):
            self.adopt_existing_ffmpeg()
        self.recover_pending_recording_sessions()
        self._stop_unwanted_live_recordings()
        self.thread = threading.Thread(target=self._run, name="live-monitor", daemon=True)
        self.thread.start()
        self.emit("engine", t("monitoring_started"))

    def stop(self, terminate_recordings=True):
        self.stop_event.set()
        self.wake_event.set()
        # FIX-APP-12: Join monitor thread (bounded) before teardown to prevent
        # races between stop() and _poll_processes()/_check_profile().
        if hasattr(self, "thread") and self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        if terminate_recordings:
            for profile_id, process in list(self.processes.items()):
                if process.poll() is None:
                    self.emit(profile_id, "Stopping active recording.")
                    recording = self.recordings.get(profile_id, {})
                    recording["finalize_on_exit"] = True
                    recording["stop_reason"] = "Recorder stopped"
                    process.terminate()
            deadline = time.time() + 5
            while time.time() < deadline and any(
                process.poll() is None for process in self.processes.values()
            ):
                time.sleep(0.05)
            # FIX-H3: Force-kill any survivors that didn't respond to terminate().
            for profile_id, process in list(self.processes.items()):
                if process.poll() is None:
                    logging.warning("Force-killing unresponsive FFmpeg for %s (PID %s)", profile_id, getattr(process, "pid", "?"))
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(process.pid), "/T"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10, **hidden_subprocess_kwargs(),
                        )
                    except Exception:
                        pass
            self._poll_processes()
            for profile_id in list(self.recordings):
                if profile_id not in self.processes:
                    self._finalize_recording_session(profile_id, "Recorder stopped")
        self.emit("engine", t("monitoring_stopping"))

    def stop_profile_recording(self, profile_id, reason="Live recording disabled"):
        process = self.processes.get(profile_id)
        if process is not None and process.poll() is None:
            self.emit(profile_id, reason)
            recording = self.recordings.get(profile_id) or {}
            recording["finalize_on_exit"] = True
            recording["stop_reason"] = reason
            try:
                process.terminate()
            except Exception:
                logging.exception("Failed to stop recording for %s", profile_id)
            deadline = time.time() + 5
            while time.time() < deadline and process.poll() is None:
                time.sleep(0.05)
            if process.poll() is None:
                logging.warning(
                    "Force-killing unresponsive FFmpeg for %s (PID %s)",
                    profile_id,
                    getattr(process, "pid", "?"),
                )
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(process.pid), "/T"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        **hidden_subprocess_kwargs(),
                    )
                except Exception:
                    logging.exception("Failed to force-kill FFmpeg for %s", profile_id)
        self._poll_processes()
        if profile_id in self.recordings and profile_id not in self.processes:
            self._finalize_recording_session(profile_id, reason)

    def _stop_unwanted_live_recordings(self):
        for profile in list(self.store.profiles):
            if wants_live_recording(profile):
                continue
            profile_id = profile["id"]
            if self._is_recording(profile_id) or self._has_recording_session(profile_id):
                self.stop_profile_recording(profile_id, t("live_recording_off_detail"))

    def refresh_all(self):
        for profile in list(self.store.profiles):
            self.next_check[profile["id"]] = 0
        if not self.is_running():
            self.start()
        self.wake_event.set()
        self.emit("engine", t("refresh_requested"))

    def emit(self, profile_id, message, **state):
        self.event_queue.put({"profile_id": profile_id, "message": message, "state": state, "time": now_text()})

    @staticmethod
    def interval_seconds(profile):
        try:
            return max(15, int(profile.get("poll_interval_seconds", DEFAULT_NEW_PROFILE_INTERVAL)))
        except (TypeError, ValueError):
            return DEFAULT_NEW_PROFILE_INTERVAL

    def effective_interval_seconds(self, profile_id, profile):
        base_interval = self.interval_seconds(profile)
        errors = self.error_counts.get(profile_id, 0)
        if errors <= 0:
            return base_interval
        error_kind = self.last_error_kinds.get(profile_id)
        if error_kind == "risk_control":
            key = "priority_risk_control_backoff_seconds" if profile.get("priority") else "standard_risk_control_backoff_seconds"
            return self.setting_seconds(key)
        if error_kind == "not_visible":
            return self.setting_seconds("not_visible_backoff_seconds")
        if error_kind == "unsupported_stream":
            return self.setting_seconds("unsupported_stream_backoff_seconds")
        if error_kind == "captcha":
            # FIX-APP-2: Escalate captcha backoff exponentially (was flat 120s forever)
            base = self.setting_seconds("captcha_backoff_seconds")
            error_max = self.setting_seconds("error_max_backoff_seconds")
            return min(base * (2 ** min(errors - 1, 4)), error_max)
        error_min = self.setting_seconds("error_min_backoff_seconds")
        error_max = self.setting_seconds("error_max_backoff_seconds")
        return min(max(base_interval, error_min) * (2 ** (errors - 1)), error_max)

    def setting_seconds(self, key):
        fallback = default_settings()[key]
        try:
            return max(1, int(self.store.settings.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    def jittered_wait_seconds(self, profile_id, profile, base_seconds):
        try:
            jitter_key = "priority_poll_jitter_seconds" if profile.get("priority") else "poll_jitter_seconds"
            max_jitter = max(0, int(self.store.settings.get(jitter_key, default_settings()[jitter_key])))
        except (TypeError, ValueError, KeyError):
            max_jitter = 0
        if max_jitter <= 0:
            return int(base_seconds)
        return int(base_seconds) + random.randint(0, max_jitter)

    @staticmethod
    def media_suffixes():
        return {".flv", ".mkv", ".mp4", ".ts"}

    def _active_ffmpeg_processes(self):
        if os.name != "nt":
            return []
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name = 'ffmpeg.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                timeout=10,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            logging.warning("Could not scan FFmpeg processes: %s", exc)
            return []
        if result.returncode != 0 or not result.stdout.strip():
            if result.stderr.strip():
                logging.warning("FFmpeg process scan failed: %s", result.stderr.strip())
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logging.warning("Could not parse FFmpeg process scan: %s", exc)
            return []
        if isinstance(payload, dict):
            payload = [payload]
        return [item for item in payload if item.get("CommandLine")]

    @staticmethod
    def _normalize_command_path(value):
        return str(value or "").replace("/", "\\").lower()

    def _output_from_command_line(self, command_line, output_dir):
        try:
            parts = shlex.split(command_line, posix=False)
        except ValueError:
            parts = command_line.split()
        for part in reversed(parts):
            candidate = part.strip().strip('"')
            if not candidate:
                continue
            candidate_path = Path(candidate)
            if candidate_path.suffix.lower() not in self.media_suffixes():
                continue
            try:
                candidate_path = os.path.normcase(os.path.abspath(candidate))
                output_root = os.path.normcase(os.path.abspath(output_dir))
                if os.path.commonpath((candidate_path, output_root)) == output_root:
                    return candidate
            except (OSError, ValueError):
                continue
        return ""

    @staticmethod
    def _read_recording_manifest(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _manifest_payload(recording):
        keys = (
            "profile_id",
            "profile_name",
            "final_output",
            "session_dir",
            "manifest_path",
            "stderr_path",
            "started_at",
            "parts",
            "part_index",
            "status",
            "last_exit_code",
            "stop_reason",
        )
        return {key: recording.get(key) for key in keys}

    def _save_recording_manifest(self, recording):
        manifest_path = recording.get("manifest_path")
        if not manifest_path:
            return
        path = Path(manifest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self._manifest_payload(recording), fh, ensure_ascii=False, indent=2)
        temporary.replace(path)

    @staticmethod
    def _parts_size(recording):
        total = 0
        for value in recording.get("parts") or []:
            try:
                total += Path(value).stat().st_size
            except OSError:
                continue
        return total

    def _new_recording_session(self, profile, stream):
        output_dir = Path(profile["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = output_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        anchor = safe_name(stream.anchor_name or profile["name"])
        title = safe_name(stream.title or "live")
        container = str(profile.get("container") or self.store.settings.get("container") or "mkv").lower()
        if container != "mkv":
            container = "mkv"
        stem = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{anchor}_{title}"
        final_output = output_dir / f"{stem}.{container}"
        session_dir = output_dir / ".recording_sessions" / f"{stem}_{uuid.uuid4().hex[:8]}"
        session_dir.mkdir(parents=True, exist_ok=False)
        recording = {
            "profile_id": profile["id"],
            "profile_name": profile.get("name") or anchor,
            "final_output": str(final_output),
            "output_file": "",
            "session_dir": str(session_dir),
            "manifest_path": str(session_dir / "session.json"),
            "stderr_path": str(logs_dir / f"{stem}.ffmpeg.log"),
            "started_at": time.time(),
            "parts": [],
            "part_index": 0,
            "status": "recording",
            "last_size": 0,
            "last_growth_at": time.time(),
            "offline_since": None,
            "offline_confirmations": 0,
        }
        self.recordings[profile["id"]] = recording
        self._save_recording_manifest(recording)
        return recording

    def _recover_session_manifest(self, profile_id, output_file):
        manifest_path = Path(output_file).parent / "session.json"
        manifest = self._read_recording_manifest(manifest_path)
        if not manifest or manifest.get("profile_id") != profile_id:
            return None
        manifest["output_file"] = output_file
        manifest["manifest_path"] = str(manifest_path)
        manifest["session_dir"] = str(manifest_path.parent)
        manifest.setdefault("parts", [])
        if output_file not in manifest["parts"]:
            manifest["parts"].append(output_file)
        manifest.setdefault("started_at", time.time())
        manifest.setdefault("last_growth_at", time.time())
        manifest.setdefault("last_size", 0)
        manifest["status"] = "recording"
        return manifest

    def adopt_existing_ffmpeg(self):
        adopted = 0
        for process_info in self._active_ffmpeg_processes():
            pid = process_info.get("ProcessId")
            command_line = process_info.get("CommandLine") or ""
            for profile in self.store.profiles:
                profile_id = profile["id"]
                if profile_id in self.processes:
                    continue
                output_dir = profile.get("output_dir", "")
                if not output_dir:
                    continue
                output_file = self._output_from_command_line(command_line, output_dir)
                if not output_file:
                    continue
                stderr_path = str(Path(output_dir) / "logs" / f"{Path(output_file).stem}.ffmpeg.log")
                try:
                    output_stat = Path(output_file).stat()
                    started_at = output_stat.st_ctime
                    last_size = output_stat.st_size
                except OSError:
                    started_at = time.time()
                    last_size = 0
                process = AdoptedProcess(int(pid))
                self.processes[profile_id] = process
                recovered = self._recover_session_manifest(profile_id, output_file)
                self.recordings[profile_id] = recovered or {
                    "output_file": output_file,
                    "stderr_path": stderr_path,
                    "started_at": started_at,
                    "pid": process.pid,
                    "adopted": True,
                    "last_size": last_size,
                    "last_growth_at": time.time(),
                }
                self.recordings[profile_id]["pid"] = process.pid
                self.emit(
                    profile_id,
                    f"Adopted active FFmpeg recording: {Path(output_file).name}",
                    status="Recording",
                    recording=True,
                    current_file=output_file,
                    file_size=file_size_text(output_file),
                    pid=process.pid,
                    elapsed=seconds_text(time.time() - started_at),
                    last_warning=last_log_line(stderr_path),
                    cooldown="Recording",
                    next_check="Recording",
                )
                logging.info("Adopted FFmpeg PID %s for %s", process.pid, profile.get("name"))
                adopted += 1
                break
        if adopted:
            self.emit("engine", f"Adopted {adopted} active FFmpeg recording(s).")

    def recover_pending_recording_sessions(self):
        recovered = 0
        for profile in self.store.profiles:
            profile_id = profile["id"]
            if profile_id in self.recordings:
                continue
            sessions_root = Path(profile.get("output_dir") or "") / ".recording_sessions"
            if not sessions_root.exists():
                continue
            def _safe_mtime(p):  # FIX-APP-3: avoid TOCTOU race on stat()
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0
            manifests = sorted(
                sessions_root.glob("*/session.json"),
                key=_safe_mtime,
                reverse=True,
            )
            for manifest_path in manifests:
                manifest = self._read_recording_manifest(manifest_path)
                if not manifest or manifest.get("profile_id") != profile_id:
                    continue
                if manifest.get("status") in {"complete", "failed"}:
                    continue
                parts = [str(Path(value)) for value in manifest.get("parts") or [] if Path(value).exists()]
                if not parts:
                    continue
                manifest["parts"] = parts
                manifest["manifest_path"] = str(manifest_path)
                manifest["session_dir"] = str(manifest_path.parent)
                manifest["output_file"] = parts[-1]
                manifest["started_at"] = float(manifest.get("started_at") or time.time())
                manifest["last_size"] = Path(parts[-1]).stat().st_size
                manifest["last_growth_at"] = time.time()
                manifest["status"] = "recovering"
                manifest["recovery_started_at"] = time.time()
                manifest["offline_since"] = None
                manifest["offline_confirmations"] = 0
                self.recordings[profile_id] = manifest
                self.next_check[profile_id] = 0
                self._save_recording_manifest(manifest)
                self.emit(
                    profile_id,
                    f"Recovered unfinished MKV session with {len(parts)} part(s); refreshing the live URL.",
                    status="Recovering",
                    recording=False,
                    current_file=manifest.get("final_output", ""),
                    file_size=byte_size_text(self._parts_size(manifest)),
                    next_check="Now",
                    cooldown="Crash recovery",
                )
                recovered += 1
                break
        if recovered:
            self.emit("engine", f"Recovered {recovered} unfinished MKV recording session(s).")

    @staticmethod
    def classify_error(exc):
        message = str(exc)
        lowered = message.lower()
        # FIX-C2: Removed bare "verification" which caused captcha false positives.
        # Require specific captcha markers only.
        # FIX-M7: Tightened captcha classification to avoid false positives.
        # "verify.douyin" is specific enough; bare "verify" would match SSL errors.
        if (
            "captcha" in lowered
            or "\u9a8c\u8bc1\u7801" in message
            or "\u8bf7\u5b8c\u6210\u9a8c\u8bc1" in message
            or "verify.douyin" in lowered
            or ("slide" in lowered and "verify" in lowered)
            or "verification required" in lowered
            or "human verification" in lowered
            or "sec.douyin.com" in lowered  # FIX-APP-10: captcha CDN domain
            or "action=verify" in lowered  # FIX-APP-10: captcha action parameter
        ):
            return "captcha", t("captcha")
        # FIX-C1: Replaced mojibake literals with correct Chinese characters.
        if (
            "risk control" in lowered
            or "\u98ce\u63a7" in message
            or "\u98ce\u9669\u9650\u5236" in message
            or "\u8bbf\u95ee\u9891\u7e41" in message
            or "\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41" in message
            or "\u670d\u52a1\u5668\u6253\u77e1\u4e86" in message
        ):
            return "risk_control", t("rate_limited")
        if "\u53ef\u89c1\u8303\u56f4" in message:
            return "not_visible", t("not_visible")
        if "VR live is not supported" in message:
            return "unsupported_stream", t("unsupported_stream")
        if "empty api" in lowered or "empty response" in lowered:
            return "empty_response", t("empty_response")
        return "error", t("error")

    def _run(self):
        while not self.stop_event.is_set():
            self._poll_processes()
            now = time.monotonic()
            profiles = sorted(
                list(self.store.profiles),
                key=lambda item: (not item.get("priority", False), item.get("name", "").lower()),
            )
            for profile in profiles:
                if self.stop_event.is_set():
                    break
                profile_id = profile["id"]
                if self.next_check.get(profile_id, 0) > now:
                    continue
                if not profile.get("enabled", True):
                    wait_seconds = self.interval_seconds(profile)
                    self.emit(
                        profile_id,
                        "Disabled.",
                        status="Disabled",
                        recording=False,
                        cooldown="Disabled",
                        next_check=future_text(wait_seconds),
                    )
                    self.next_check[profile_id] = time.monotonic() + wait_seconds
                    continue
                if not profile.get("record_live", True):
                    if self._is_recording(profile_id) or self._has_recording_session(profile_id):
                        self.stop_profile_recording(profile_id, t("live_recording_off_detail"))
                    wait_seconds = self.interval_seconds(profile)
                    self.emit(
                        profile_id,
                        t("live_recording_off_detail"),
                        status="Live off",
                        recording=False,
                        cooldown=t("live_recording_off"),
                    )
                    self.next_check[profile_id] = time.monotonic() + wait_seconds
                    continue
                if self._is_recording(profile_id):
                    wait_seconds = self.interval_seconds(profile)
                    self.emit(profile_id, "Recording.", status="Recording", recording=True, **self.recording_snapshot(profile_id))
                    self.next_check[profile_id] = time.monotonic() + wait_seconds
                    continue
                self._check_profile(profile)
                wait_seconds = self.jittered_wait_seconds(profile_id, profile, self.effective_interval_seconds(profile_id, profile))
                if self._has_recording_session(profile_id) and not self._is_recording(profile_id):
                    wait_seconds = min(wait_seconds, 10)
                self.next_check[profile_id] = time.monotonic() + wait_seconds
                if self._is_recording(profile_id):
                    self.emit(profile_id, "Recording.", next_check="Recording", cooldown="Recording", **self.recording_snapshot(profile_id))
                    continue
                self.emit(
                    profile_id,
                    t("next_check_in", seconds=wait_seconds),
                    next_check=future_text(wait_seconds),
                    cooldown=self.cooldown_label(profile_id, profile, wait_seconds),
                )
            self.wake_event.wait(1)
            self.wake_event.clear()

        self._poll_processes()
        self.emit("engine", t("monitoring_stopped"))

    def _is_recording(self, profile_id):
        process = self.processes.get(profile_id)
        return bool(process and process.poll() is None)

    def _has_recording_session(self, profile_id):
        recording = self.recordings.get(profile_id) or {}
        return bool(recording.get("session_dir"))

    def _poll_processes(self):
        for profile_id, process in list(self.processes.items()):
            if process.poll() is None:
                recording = self.recordings.get(profile_id, {})
                rotate_at = recording.get("rotate_at")
                if rotate_at and time.time() >= rotate_at and not recording.get("rotation_stop_requested"):
                    recording["rotation_stop_requested"] = True
                    recording["stop_reason"] = "Scheduled signed-URL refresh"
                    self.emit(
                        profile_id,
                        "Refreshing the signed live URL into a new MKV part.",
                        status="Refreshing URL",
                        recording=True,
                        next_check="Now",
                        cooldown="Scheduled URL refresh",
                        **self.recording_snapshot(profile_id),
                    )
                    logging.info("Refreshing signed live URL for %s; terminating current MKV part.", profile_id)
                    try:
                        process.terminate()
                    except Exception:
                        logging.exception("Failed to rotate FFmpeg for %s", profile_id)
                    continue
                if self._recording_stalled(profile_id):
                    if recording.get("stall_stop_requested"):
                        continue
                    timeout = self.setting_seconds("recording_stall_timeout_seconds")
                    recording["stall_stop_requested"] = True
                    snapshot = self.recording_snapshot(profile_id)
                    self.next_check[profile_id] = 0
                    self.emit(
                        profile_id,
                        f"Recording stalled for {timeout}s; restarting stream check.",
                        status="Rechecking",
                        recording=False,
                        next_check="Now",
                        cooldown="Recording stalled",
                        **snapshot,
                    )
                    logging.warning("Recording stalled for %ss on %s; terminating FFmpeg.", timeout, profile_id)
                    recording["stop_reason"] = f"Recording stalled for {timeout}s"
                    try:
                        process.terminate()
                    except Exception:
                        logging.exception("Failed to terminate stalled FFmpeg for %s", profile_id)
                continue
            stderr_file = self.stderr_files.pop(profile_id, None)
            if stderr_file:
                stderr_file.close()
            snapshot = self.recording_snapshot(profile_id)
            last_warning = snapshot.get("last_warning") or ""
            if last_warning.startswith("=== MKV part "):
                last_warning = ""
                snapshot["last_warning"] = ""
            if last_warning:
                logging.warning(
                    "Recording ended with code %s for %s. Last FFmpeg warning: %s",
                    process.returncode,
                    profile_id,
                    last_warning,
                )
            else:
                logging.info("Recording ended with code %s for %s.", process.returncode, profile_id)
            self.processes.pop(profile_id, None)
            recording = self.recordings.get(profile_id, {})
            if not recording.get("session_dir"):
                self.recordings.pop(profile_id, None)
                self.next_check[profile_id] = 0
                self.emit(
                    profile_id,
                    f"Recording ended with code {process.returncode}; rechecking stream.",
                    status="Rechecking",
                    recording=False,
                    next_check="Now",
                    cooldown="Recording ended",
                    **snapshot,
                )
                continue
            recording["pid"] = ""
            recording["last_exit_code"] = process.returncode
            recording["segment_ended_at"] = time.time()
            recording.setdefault("recovery_started_at", time.time())  # FIX-M12: Don't reset on every segment end
            recording["offline_since"] = None
            recording["offline_confirmations"] = 0
            recording["status"] = "recovering"
            recording.pop("stall_stop_requested", None)
            recording.pop("rotation_stop_requested", None)
            try:  # FIX-APP-5: prevent I/O error from killing monitor thread
                self._save_recording_manifest(recording)
            except OSError:
                logging.exception("Could not save manifest for %s during recovery", profile_id)
            # FIX-S3: Count rapid ffmpeg deaths (<30s runtime) as errors
            # to prevent infinite 10s spawn/kill churn on dead CDN URLs.
            segment_started = recording.get("segment_started_at") or 0
            segment_duration = time.time() - segment_started
            if segment_duration < 30 and not recording.get("finalize_on_exit"):
                self.error_counts[profile_id] = self.error_counts.get(profile_id, 0) + 1
                logging.warning(
                    "FFmpeg for %s died after only %.0fs (part %s); incrementing error count to %d.",
                    profile_id, segment_duration, recording.get("part_index"),
                    self.error_counts[profile_id],
                )
            else:
                self.error_counts[profile_id] = 0
            self.next_check[profile_id] = 0
            if recording.pop("finalize_on_exit", False):
                self._finalize_recording_session(
                    profile_id,
                    recording.get("stop_reason") or "Recorder stopped",
                )
                continue
            self.emit(
                profile_id,
                f"MKV part ended with code {process.returncode}; refreshing the live URL.",
                status="Rechecking",
                recording=False,
                next_check="Now",
                cooldown="Refreshing URL",
                **snapshot,
            )

    def _recording_stalled(self, profile_id):
        recording = self.recordings.get(profile_id, {})
        output_file = recording.get("output_file")
        if not output_file:
            return False
        try:
            current_size = Path(output_file).stat().st_size
        except OSError:
            return False
        now = time.time()
        last_size = recording.get("last_size")
        if last_size is None or current_size > last_size:
            recording["last_size"] = current_size
            recording["last_growth_at"] = now
            recording.pop("stall_stop_requested", None)
            return False
        recording["last_size"] = current_size
        last_growth_at = recording.get("last_growth_at") or now
        timeout = self.setting_seconds("recording_stall_timeout_seconds")
        return now - last_growth_at >= timeout

    def recording_snapshot(self, profile_id):
        recording = self.recordings.get(profile_id, {})
        started_at = recording.get("started_at")
        elapsed = seconds_text(time.time() - started_at) if started_at else ""
        total_size = self._parts_size(recording) if recording.get("session_dir") else None
        return {
            "current_file": recording.get("final_output") or recording.get("output_file", ""),
            "file_size": byte_size_text(total_size) if total_size is not None else file_size_text(recording.get("output_file")),
            "pid": recording.get("pid", ""),
            "elapsed": elapsed,
            "last_warning": last_log_line(recording.get("stderr_path")),
        }

    @staticmethod
    def _concat_file_line(path):
        normalized = Path(path).resolve().as_posix().replace("'", "'\\''")
        return "file '" + normalized + "'"

    def _validate_final_recording(self, path):
        probe_setting = self.store.settings.get("ffprobe_path") or str(DEFAULT_FFPROBE_PATH)
        try:
            probe_path = resolve_trusted_executable(
                probe_setting,
                allowed_basenames={"ffprobe.exe"},
                trusted_roots=_trusted_tool_roots(),
            )
        except ValueError:
            probe_path = ""
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError("Final MKV was not created or is empty")
        if not probe_path:
            return
        result = subprocess.run(
            [
                probe_path,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=60,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ffprobe could not validate the final MKV")
        payload = json.loads(result.stdout or "{}")
        stream_types = {item.get("codec_type") for item in payload.get("streams") or []}
        if not stream_types.intersection({"audio", "video"}):
            raise RuntimeError("Final MKV contains no audio or video streams")

    def _finalize_recording_worker(self, recording, reason):
        final_output = Path(recording["final_output"])
        session_dir = Path(recording["session_dir"])
        parts = [Path(value) for value in recording.get("parts") or [] if Path(value).exists() and Path(value).stat().st_size]
        profile_id = recording["profile_id"]
        if not parts:
            recording["status"] = "failed"
            recording["stop_reason"] = f"{reason}: no usable MKV parts"
            self._save_recording_manifest(recording)
            self.emit(profile_id, "Recording finalization failed: no usable MKV parts.", status="Finalization failed", recording=False)
            return
        concat_path = session_dir / "parts.txt"
        temporary_output = final_output.with_name(final_output.stem + ".finalizing.mkv")
        finalize_log = session_dir / "finalize.log"
        try:
            with open(concat_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self._concat_file_line(path) for path in parts) + "\n")
            recording["status"] = "finalizing"
            recording["stop_reason"] = reason
            self._save_recording_manifest(recording)
            command = [
                resolve_ffmpeg_executable(self.store.settings["ffmpeg_path"]),
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "warning",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-fflags",
                "+genpts",
                "-avoid_negative_ts",
                "make_zero",
                str(temporary_output),
            ]
            with open(finalize_log, "w", encoding="utf-8") as log_handle:
                result = subprocess.run(
                    command,
                    cwd=str(session_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_handle,
                    timeout=None,
                    **hidden_subprocess_kwargs(),
                )
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg concat exited with code {result.returncode}")
            self._validate_final_recording(temporary_output)
            final_output.parent.mkdir(parents=True, exist_ok=True)
            temporary_output.replace(final_output)
            recording["status"] = "complete"
            self._save_recording_manifest(recording)
            # FIX-H5: Move rmtree outside the critical path. If cleanup fails
            # (locked file, antivirus), the recording is still successful.
            try:
                shutil.rmtree(session_dir)
            except OSError:
                logging.warning("Could not clean up session dir %s (non-fatal)", session_dir)
            self.emit(
                profile_id,
                f"Recording finalized: {final_output.name} ({len(parts)} MKV part{'s' if len(parts) != 1 else ''}).",
                status="Offline",
                recording=False,
                current_file=str(final_output),
                file_size=file_size_text(final_output),
                cooldown="Complete",
            )
            logging.info("Finalized %s from %s MKV part(s): %s", profile_id, len(parts), final_output)
        except Exception as exc:
            recording["status"] = "failed"
            recording["stop_reason"] = f"{reason}: {exc}"
            try:
                self._save_recording_manifest(recording)
            except Exception:
                logging.exception("Could not update failed recording manifest for %s", profile_id)
            logging.exception("Could not finalize MKV session for %s", profile_id)
            self.emit(
                profile_id,
                f"Recording finalization failed; raw MKV parts were preserved: {exc}",
                status="Finalization failed",
                recording=False,
                current_file=str(session_dir),
                cooldown="Raw parts preserved",
            )

    def _finalize_recording_session(self, profile_id, reason):
        recording = self.recordings.pop(profile_id, None)
        if not recording or not recording.get("session_dir"):
            return
        recording["status"] = "finalizing"
        recording["stop_reason"] = reason
        self._save_recording_manifest(recording)
        self.emit(
            profile_id,
            f"Finalizing {len(recording.get('parts') or [])} MKV part(s) into one recording.",
            status="Finalizing",
            recording=False,
            current_file=recording.get("final_output", ""),
            cooldown="Finalizing",
        )
        thread = threading.Thread(
            target=self._finalize_recording_worker,
            args=(recording, reason),
            name=f"recording-finalizer-{profile_id}",
            daemon=False,
        )
        self.finalizer_threads = [item for item in self.finalizer_threads if item.is_alive()]
        self.finalizer_threads.append(thread)
        thread.start()

    def cooldown_label(self, profile_id, profile, wait_seconds):
        errors = self.error_counts.get(profile_id, 0)
        if errors <= 0:
            return "Normal"
        error_kind = self.last_error_kinds.get(profile_id, "error")
        if error_kind == "risk_control":
            label = "Priority risk cooldown" if profile.get("priority") else "Normal risk cooldown"
        elif error_kind == "not_visible":
            label = "Visible access range"
        elif error_kind == "unsupported_stream":
            label = "Unsupported stream"
        else:
            label = "Error backoff"
        return f"{label} ({wait_seconds}s, x{errors})"

    def _check_profile(self, profile):
        try:
            # FIX-APP-4: 30s timeout prevents a hanging Douyin API from blocking all profiles
            room, stream = asyncio.run(asyncio.wait_for(self._resolve(profile), timeout=30))
            status = t("live") if stream.is_live else t("offline")
            live_url = room.get("live_url") or ""
            if live_url and profile.get("fallback_live_url") != live_url:
                profile["fallback_live_url"] = live_url
                # FIX-APP-8: Only save profiles (not settings) to reduce I/O
                save_json(PROFILES_FILE, self.store.profiles)
            self.emit(
                profile["id"],
                t("checked", status=status),
                status=status,
                recording=False,
                last_checked=now_text(),
                live_url=live_url,
                title=room.get("title") or "",
                anchor_name=room.get("anchor_name") or profile.get("name", ""),
                pid="",
                elapsed="",
            )
            recording = self.recordings.get(profile["id"])
            if stream.is_live and has_recording_url(stream):
                if recording and recording.get("session_dir"):
                    recording["offline_since"] = None
                    recording["offline_confirmations"] = 0
                    recording["recovery_started_at"] = None
                self._start_recording(profile, stream)
            elif recording and recording.get("session_dir"):
                now = time.time()
                if not recording.get("offline_since"):
                    recording["offline_since"] = now
                    recording["offline_confirmations"] = 0
                recording["offline_confirmations"] = int(recording.get("offline_confirmations") or 0) + 1
                grace = self.setting_seconds("recording_offline_grace_seconds")
                offline_for = now - recording["offline_since"]
                recording["status"] = "offline_grace"
                self._save_recording_manifest(recording)
                if offline_for >= grace and recording["offline_confirmations"] >= 2:
                    self._finalize_recording_session(profile["id"], "Live confirmed offline")
                else:
                    remaining = max(0, grace - int(offline_for))
                    self.emit(
                        profile["id"],
                        f"Live appears offline; holding the MKV session open for {remaining}s.",
                        status="Confirming offline",
                        recording=False,
                        next_check="Soon",
                        cooldown="Offline grace",
                        **self.recording_snapshot(profile["id"]),
                    )
            self.error_counts[profile["id"]] = 0
            self.last_error_kinds.pop(profile["id"], None)
        except Exception as exc:
            profile_id = profile["id"]
            recording = self.recordings.get(profile_id)
            if recording and recording.get("session_dir") and not self._is_recording(profile_id):
                recording["status"] = "recovering"
                recording.setdefault("recovery_started_at", time.time())
                recording["stop_reason"] = f"URL refresh failed: {exc}"
                # FIX-R1: Bound recovery to 30 minutes max, then finalize and abandon.
                recovery_age = time.time() - recording["recovery_started_at"]
                if recovery_age > 1800:
                    logging.warning(
                        "Session recovery for %s exceeded 30 min; finalizing.",
                        profile_id,
                    )
                    self._finalize_recording_session(
                        profile_id,
                        f"Recovery abandoned after {int(recovery_age)}s: {exc}",
                    )
                    self.error_counts[profile_id] = self.error_counts.get(profile_id, 0) + 1
                    error_kind, status = self.classify_error(exc)
                    self.last_error_kinds[profile_id] = error_kind
                    backoff = self.effective_interval_seconds(profile_id, profile)
                    self.emit(
                        profile_id,
                        f"{status}: {exc}. Backing off {backoff}s.",
                        status=status,
                        recording=False,
                        last_checked=now_text(),
                        cooldown=self.cooldown_label(profile_id, profile, backoff),
                        next_check=f"{backoff}s",
                    )
                    return
                self._save_recording_manifest(recording)
                logging.warning("Live URL refresh failed for active session %s: %s", profile_id, exc)
                self.emit(
                    profile_id,
                    f"Could not refresh the live URL yet; keeping MKV parts open: {exc}",
                    status="Recovering",
                    recording=False,
                    last_checked=now_text(),
                    cooldown="Retrying URL refresh",
                    next_check="Soon",
                    **self.recording_snapshot(profile_id),
                )
                return
            self.error_counts[profile_id] = self.error_counts.get(profile_id, 0) + 1
            error_kind, status = self.classify_error(exc)
            self.last_error_kinds[profile_id] = error_kind
            backoff = self.effective_interval_seconds(profile_id, profile)
            if error_kind in {"captcha", "risk_control", "not_visible", "unsupported_stream", "empty_response"}:
                logging.warning("Profile check warning for %s: %s", profile.get("name"), exc)
            else:
                logging.exception("Profile check failed for %s", profile.get("name"))
            self.emit(
                profile_id,
                f"{status}: {exc}. Backing off {backoff}s.",
                status=status,
                recording=False,
                last_checked=now_text(),
                cooldown=self.cooldown_label(profile_id, profile, backoff),
                next_check=future_text(backoff),
            )

    async def _resolve(self, profile):
        if profile.get("platform") == "youtube" or detect_platform(profile.get("url", "")) == "youtube":
            return self._resolve_youtube(profile)
        return await self._resolve_douyin(profile)

    async def _resolve_douyin(self, profile):
        live = DouyinLiveStream(
            proxy_addr=profile.get("proxy_addr") or None,
            cookies=None,
            stream_orientation=int(profile.get("stream_orientation") or 1),
        )
        url = profile["url"].strip()
        if "live.douyin.com/" in url:
            room = await live.fetch_web_stream_data(url)
        else:
            fallback_url = profile.get("fallback_live_url", "").strip()
            if fallback_url:
                try:
                    room = await live.fetch_web_stream_data(fallback_url)
                except Exception:
                    room = await live.fetch_app_stream_data(url)
            else:
                room = await live.fetch_app_stream_data(url)
        stream = await live.fetch_stream_url(room, profile.get("quality") or self.store.settings["quality"])
        return room, stream

    def _resolve_youtube(self, profile):
        url = self._youtube_probe_url(profile["url"].strip())
        metadata = self._run_ytdlp_json(profile, url)
        if metadata is None:
            room = {
                "live_url": profile["url"].strip(),
                "title": "",
                "anchor_name": profile.get("name", ""),
            }
            stream = SimpleNamespace(is_live=False, record_url="", flv_url="", title="", anchor_name=profile.get("name", ""))
            return room, stream

        is_live = bool(metadata.get("is_live")) or metadata.get("live_status") == "is_live"
        uploader = metadata.get("uploader") or metadata.get("channel") or profile.get("name", "YouTube")
        title = metadata.get("title") or "live"
        watch_url = metadata.get("webpage_url") or profile["url"].strip()
        stream_url = metadata.get("url") if is_live else ""
        if is_live and not stream_url:
            stream_url = self._pick_youtube_format_url(metadata)

        room = {"live_url": watch_url, "title": title, "anchor_name": uploader}
        stream = SimpleNamespace(
            is_live=is_live and bool(stream_url),
            record_url=stream_url,
            flv_url="",
            title=title,
            anchor_name=uploader,
        )
        return room, stream

    def _youtube_probe_url(self, url):
        cleaned = url.split("?", 1)[0].rstrip("/")
        lowered = cleaned.lower()
        if "youtube.com/watch" in lowered or "youtu.be/" in lowered or lowered.endswith("/live"):
            return url
        if "youtube.com/" in lowered:
            return cleaned + "/live"
        return url

    def _run_ytdlp_json(self, profile, url):
        cmd = [
            resolve_ytdlp_executable(self.store.settings.get("ytdlp_path", str(DEFAULT_YTDLP_PATH))),
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            "--skip-download",
            "-f",
            "best[protocol^=m3u8]/best",
            url,
        ]
        cookies = (profile.get("cookies") or "").strip()
        if cookies:
            cookie_path = Path(cookies)
            if cookie_path.is_file():
                cmd[1:1] = ["--cookies", str(cookie_path)]
            else:
                logging.warning(
                    "Ignoring YouTube cookies value that is not an existing Netscape cookie file."
                )
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=45,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            offline_markers = (
                "not currently live",
                "this live event will begin",
                "premiere",
                "no video formats found",
                "is offline",
                "this video is unavailable",
            )
            if any(marker in stderr.lower() for marker in offline_markers):
                return None
            raise RuntimeError(stderr or "yt-dlp could not resolve this YouTube link")
        return json.loads(result.stdout.lstrip("\ufeff"))

    def _pick_youtube_format_url(self, metadata):
        formats = metadata.get("formats") or []
        for fmt in reversed(formats):
            url = fmt.get("url")
            protocol = fmt.get("protocol") or ""
            if url and ("m3u8" in protocol or ".m3u8" in url):
                return url
        for fmt in reversed(formats):
            if fmt.get("url"):
                return fmt["url"]
        return ""

    def _start_recording(self, profile, stream):
        # FIX-P1: Abort if shutdown was requested to prevent orphaned ffmpeg.
        if self.stop_event.is_set():
            logging.info("Skipping recording start for %s; shutdown in progress.", profile.get("name"))
            return
        output_dir = Path(profile["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        input_url, stream_kind = recording_input_url(stream)
        if not input_url:
            raise RuntimeError("Live stream did not include a recording URL")
        if not is_safe_recording_url(input_url):
            raise RuntimeError("Live stream URL uses an unsupported or unsafe protocol")
        recording = self.recordings.get(profile["id"])
        is_resume = bool(recording and recording.get("session_dir"))
        if not is_resume:
            recording = self._new_recording_session(profile, stream)
        recording["part_index"] = int(recording.get("part_index") or 0) + 1
        part_path = Path(recording["session_dir"]) / f"part-{recording['part_index']:04d}.mkv"
        stderr_path = Path(recording["stderr_path"])
        stderr_file = open(stderr_path, "a", encoding="utf-8")
        stderr_file.write(
            f"\n=== MKV part {recording['part_index']} started {datetime.now().isoformat(timespec='seconds')} "
            f"using {stream_kind} ===\n"
        )
        stderr_file.flush()
        logging.info(
            "%s: Starting MKV part %s from refreshed %s stream URL.",
            profile.get("name"),
            recording["part_index"],
            stream_kind,
        )

        cmd = [
            resolve_ffmpeg_executable(self.store.settings["ffmpeg_path"]),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-y",
            *ffmpeg_live_input_options(
                input_url,
                reconnect_delay_max=self.setting_seconds("recording_reconnect_delay_max_seconds"),
            ),
            "-i",
            input_url,
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-flush_packets",
            "1",
            str(part_path),
        ]

        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(output_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                text=True,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            stderr_file.close()
            recording["part_index"] -= 1
            if not is_resume:
                self.recordings.pop(profile["id"], None)
                shutil.rmtree(recording["session_dir"], ignore_errors=True)
            raise
        self.processes[profile["id"]] = process
        self.stderr_files[profile["id"]] = stderr_file
        # FIX-CRIT1: Close the TOCTOU race with stop(). If shutdown was requested
        # between the guard at the top and Popen, kill the process immediately.
        if self.stop_event.is_set():
            logging.warning("Shutdown detected after ffmpeg spawn for %s; killing immediately.", profile.get("name"))
            process.terminate()
            self.processes.pop(profile["id"], None)
            stderr_file.close()
            self.stderr_files.pop(profile["id"], None)
            # FIX-APP-13: Restore part_index to avoid phantom gap in sequence
            recording["part_index"] = max(0, int(recording.get("part_index") or 1) - 1)
            return
        recording["parts"].append(str(part_path))
        recording["output_file"] = str(part_path)
        recording["pid"] = process.pid
        recording["last_size"] = 0
        recording["last_growth_at"] = time.time()
        recording["segment_started_at"] = time.time()
        recording["rotate_at"] = time.time() + self.setting_seconds("recording_segment_max_seconds")
        recording["status"] = "recording"
        recording["stop_reason"] = ""
        recording.pop("recovery_started_at", None)
        self._save_recording_manifest(recording)
        self.emit(
            profile["id"],
            (
                f"Recording resumed in MKV part {recording['part_index']}: {Path(recording['final_output']).name}"
                if is_resume
                else f"Recording started: {Path(recording['final_output']).name}"
            ),
            status="Recording",
            recording=True,
            current_file=recording["final_output"],
            file_size=byte_size_text(self._parts_size(recording)),
            pid=process.pid,
            elapsed=seconds_text(time.time() - recording["started_at"]),
            last_warning="",
            cooldown="Recording",
            next_check="Recording",
        )


class MediaDownloadEngine:
    def __init__(self, store, event_queue, notify_callback=None):
        self.store = store
        self.event_queue = event_queue
        self.notify_callback = notify_callback
        self.thread = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.next_check = {}
        self.consecutive_failures = {}
        self._last_video_status = {}
        # FIX-AUDIT-5: Load persisted circuit breaker state so restarts
        # don't reset backoff for profiles that were hitting captchas.
        self._load_circuit_breaker_state()

    def _load_circuit_breaker_state(self):
        """FIX-AUDIT-5: Load persisted circuit breaker state from disk."""
        try:
            data = load_json(MEDIA_CIRCUIT_BREAKER_FILE, {})
            if isinstance(data, dict):
                saved_failures = data.get("consecutive_failures", {})
                saved_statuses = data.get("last_video_status", {})
                saved_ts = data.get("timestamp", 0)
                # Only restore state if it was saved within the last 2 hours.
                # Older state is stale and should not block fresh starts.
                if saved_ts and (time.time() - saved_ts) < 7200:
                    self.consecutive_failures = {
                        k: int(v) for k, v in saved_failures.items()
                        if isinstance(v, (int, float)) and v > 0
                    }
                    self._last_video_status = {
                        k: str(v) for k, v in saved_statuses.items()
                        if isinstance(v, str) and v
                    }
                    if self.consecutive_failures:
                        logging.info(
                            "Restored circuit breaker state: %d profile(s) with failures",
                            len(self.consecutive_failures),
                        )
        except Exception:
            logging.debug("Could not load circuit breaker state", exc_info=True)

    def _save_circuit_breaker_state(self):
        """FIX-AUDIT-5: Persist circuit breaker state to disk."""
        try:
            data = {
                "consecutive_failures": self.consecutive_failures,
                "last_video_status": self._last_video_status,
                "timestamp": time.time(),
            }
            save_json(MEDIA_CIRCUIT_BREAKER_FILE, data)
        except Exception:
            logging.debug("Could not save circuit breaker state", exc_info=True)
    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.is_running():
            return
        self.stop_event.clear()
        self.wake_event.clear()
        self.thread = threading.Thread(target=self._run, name="media-download-monitor", daemon=True)
        self.thread.start()
        self.emit("engine", t("media_monitor_started"))

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        self.emit("engine", t("media_monitor_stopping"))

    def refresh_all(self):
        for profile in list(self.store.profiles):
            self.next_check[profile["id"]] = 0
        if not self.is_running():
            self.start()
        self.wake_event.set()
        self.emit("engine", t("media_refresh_requested"))

    def refresh_profile(self, profile_id):
        self.next_check[profile_id] = 0
        if not self.is_running():
            self.start()
        self.wake_event.set()
        self.emit(profile_id, t("media_refresh_profile"))

    def emit(self, profile_id, message, **state):
        self.event_queue.put({"profile_id": profile_id, "message": message, "state": state, "time": now_text()})

    def interval_seconds(self, profile):
        try:
            return max(60, int(profile.get("media_poll_interval_seconds", self.store.settings.get("media_poll_interval_seconds", DEFAULT_MEDIA_INTERVAL))))
        except (TypeError, ValueError):
            return DEFAULT_MEDIA_INTERVAL

    @staticmethod
    def enabled_for_profile(profile):
        return bool(profile.get("auto_download_videos")) or bool(profile.get("auto_download_stories"))

    def _run(self):
        while not self.stop_event.is_set():
            now = time.monotonic()
            # OPT-4: Shuffle check order each cycle so Douyin does not see
            # a predictable fixed sequence of profile fetches.
            cycle_profiles = list(self.store.profiles)
            random.shuffle(cycle_profiles)
            first_check_done = False
            for profile in cycle_profiles:
                if self.stop_event.is_set():
                    break
                profile_id = profile["id"]
                if self.next_check.get(profile_id, 0) > now:
                    continue
                wait_seconds = self.interval_seconds(profile)
                if not profile.get("enabled", True) or not self.enabled_for_profile(profile):
                    self.next_check[profile_id] = time.monotonic() + wait_seconds
                    continue
                # OPT-2: Stagger checks with a random pause between profiles
                # to avoid burst-fetching that triggers Douyin's rate limiter.
                if first_check_done:
                    stagger = random.uniform(2, 6)  # OPT-E: reduced from 3-10s
                    self.wake_event.wait(stagger)
                    if self.stop_event.is_set():
                        break
                first_check_done = True
                self._check_profile(profile)
                # Exponential backoff on consecutive media failures.
                failures = self.consecutive_failures.get(profile_id, 0)
                # OPT-5: Circuit breaker ? if this profile just hit a captcha,
                # remaining profiles will almost certainly hit the same IP-level
                # captcha.  Skip them and let the backoff cool down.
                last_status = (self._last_video_status or {}).get(profile_id, "")
                if last_status == "captcha":
                    # FIX-CB-2: Only trip circuit breaker after 2+ consecutive captcha hits.
                    # A single captcha may be a false positive (preloaded SDK element).
                    captcha_streak = self.consecutive_failures.get(profile_id, 0)
                    if captcha_streak >= 2:
                        logging.info(
                            "Captcha circuit breaker: %s hit captcha %d times, skipping remaining profiles.",
                            profile.get("name"), captcha_streak,
                        )
                        captcha_wait = max(30, int(self.store.settings.get("captcha_backoff_seconds", 120) or 120))
                        for other in cycle_profiles:
                            oid = other["id"]
                            if self.next_check.get(oid, 0) <= time.monotonic():
                                self.next_check[oid] = time.monotonic() + captcha_wait
                        break
                    else:
                        logging.info(
                            "Captcha on %s (streak=%d), continuing with remaining profiles.",
                            profile.get("name"), captcha_streak,
                        )
                if failures > 0:
                    backoff_multiplier = min(2 ** failures, 8)
                    wait_seconds = int(wait_seconds * backoff_multiplier)
                    logging.info(
                        "Media backoff for %s: %d consecutive failures, waiting %ds",
                        profile.get("name"), failures, wait_seconds,
                    )
                self.next_check[profile_id] = time.monotonic() + wait_seconds
                self.emit(
                    profile_id,
                    f"Next media check in {wait_seconds}s.",
                    media_next_check=future_text(wait_seconds),
                )
            self.wake_event.wait(1)
            self.wake_event.clear()
        self.emit("engine", t("media_monitor_stopped"))

    @staticmethod
    def summarize_kind(summary, key):
        result = summary.get(key) or {}
        status = result.get("status") or "unknown"
        label = t("works") if key == "videos" else (t("stories") if key == "stories" else key.capitalize())
        if status == "disabled":
            return ""
        if status == "ok":
            return t(
                "summary_ok",
                label=label,
                downloaded=result.get("downloaded", 0),
                skipped=result.get("skipped", 0),
                failed=result.get("failed", 0),
            )
        if status == "no_active_stories":
            return t("stories_none")
        if status == "mobile_only":
            return t("stories_mobile")
        if status in {"blocked", "api_empty"}:
            return t("api_empty", label=label)
        if status == "login_required":
            if key == "videos":
                return t("works_login")
            return t("login_required", label=label)
        if status == "captcha":
            return t("captcha_required", label=label)
        return t("summary_status", label=label, status=status.replace("_", " "))

    def _check_profile(self, profile):
        videos = bool(profile.get("auto_download_videos"))
        stories = bool(profile.get("auto_download_stories"))
        last_progress = ""

        def on_progress(progress):
            nonlocal last_progress
            # FIX-APP-7: Abort long downloads on shutdown
            if self.stop_event.is_set():
                raise InterruptedError("Shutdown requested during media download")
            progress_text = media_progress_text(progress)
            if not progress_text or progress_text == last_progress:
                return
            last_progress = progress_text
            self.emit(
                profile["id"],
                progress_text,
                media_status=progress_text,
                media_progress=progress_text,
                media_next_check="",
            )

        try:
            summary = download_profile(
                self.store.get_profile(profile["id"]) or profile,
                self.store.settings,
                videos=videos,
                stories=stories,
                progress_callback=on_progress,
            )
            video_status = (summary.get("videos") or {}).get("status", "")
            self._last_video_status[profile["id"]] = video_status
            if video_status == "captcha" and self.notify_callback:
                try:
                    self.notify_callback(
                        f"Douyin captcha detected on {profile.get('name', 'profile')} \u2014 "
                        "media downloads paused. Open the fetch browser to solve it, "
                        "or wait for automatic retry.",
                    )
                except Exception:
                    pass
            if video_status in ("error", "api_empty", "blocked", "captcha"):
                self.consecutive_failures[profile["id"]] = self.consecutive_failures.get(profile["id"], 0) + 1
            else:
                self.consecutive_failures[profile["id"]] = 0
            self._save_circuit_breaker_state()  # FIX-AUDIT-5
            parts = [part for part in (self.summarize_kind(summary, "videos"), self.summarize_kind(summary, "stories")) if part]
            media_status = "; ".join(parts) or t("no_media_work")
            self.emit(
                profile["id"],
                t("media_check", status=media_status),
                media_status=media_status,
                media_progress=media_status,
                media_last_checked=now_text(),
                media_next_check="",
            )
        except CaptchaDetectedError as exc:
            # FIX-APP-6: CaptchaDetectedError must set captcha status for circuit breaker
            logging.warning("Media captcha detected for %s: %s", profile.get("name"), exc)
            self._last_video_status[profile["id"]] = "captcha"
            self.consecutive_failures[profile["id"]] = self.consecutive_failures.get(profile["id"], 0) + 1
            self._save_circuit_breaker_state()  # FIX-AUDIT-5
            self.emit(
                profile["id"],
                f"Media captcha: {exc}",
                media_status="Captcha detected",
                media_progress="Captcha detected",
                media_last_checked=now_text(),
                media_next_check="",
            )
        except Exception as exc:
            logging.exception("Media download check failed for %s", profile.get("name"))
            self.consecutive_failures[profile["id"]] = self.consecutive_failures.get(profile["id"], 0) + 1
            self.emit(
                profile["id"],
                f"Media error: {exc}",
                media_status=f"Error: {exc}",
                media_progress=f"Error: {exc}",
                media_last_checked=now_text(),
                media_next_check="",
            )


class AdoptedProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        # FIX-P5: Record adoption time to guard against PID reuse.
        # If the original process dies and Windows reuses the PID,
        # pid_is_running with not_started_after will correctly report "dead".
        self._adopted_at = time.time()

    def poll(self):
        if pid_is_running(self.pid, not_started_after=self._adopted_at):
            return None
        if self.returncode is None:
            # Unknown exit status for an adopted process — do not report success.
            self.returncode = 1
        return self.returncode

    def terminate(self):
        if self.poll() is not None:
            return
        # FIX-APP-11: Try graceful shutdown first (lets ffmpeg flush MKV trailer),
        # then escalate to force-kill after 3 seconds.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            pass
        # Wait up to 3s for graceful exit
        for _ in range(6):
            if self.poll() is not None:
                return
            time.sleep(0.5)
        # Escalate to force-kill
        if self.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(self.pid), "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                    **hidden_subprocess_kwargs(),
                )
            except Exception:
                pass


class ProfileDialog(Toplevel):
    def __init__(self, parent, store, profile=None):
        super().__init__(parent)
        self.store = store
        self.result = None
        self.profile = dict(profile or {})
        self.title(t("profile_title"))
        self.geometry("720x590")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.url_var = StringVar(value=self.profile.get("url", ""))
        existing_profile_url = self.profile.get("original_profile_url", "")
        if not existing_profile_url and "douyin.com/user/" in self.profile.get("url", ""):
            existing_profile_url = self.profile.get("url", "").split("?", 1)[0]
        self.profile_url_var = StringVar(value=existing_profile_url)
        self.name_var = StringVar(value=self.profile.get("name", ""))
        self.output_var = StringVar(value=self.profile.get("output_dir", ""))
        initial_quality = str(self.profile.get("quality", self.store.settings["quality"])).upper()
        self.quality_var = StringVar(value=initial_quality if initial_quality in QUALITY_OPTIONS else "OD")
        self.resolve_result_queue = queue.Queue()
        self.resolving = False
        self.resolve_after_id = None
        self.interval_var = StringVar(
            value=str(self.profile.get(
                "poll_interval_seconds",
                self.store.settings.get("new_profile_poll_interval_seconds", DEFAULT_NEW_PROFILE_INTERVAL),
            ))
        )
        self.enabled_var = BooleanVar(value=self.profile.get("enabled", True))
        self.record_live_var = BooleanVar(value=self.profile.get("record_live", True))
        self.priority_var = BooleanVar(value=self.profile.get("priority", False))
        self.auto_videos_var = BooleanVar(value=self.profile.get("auto_download_videos", False))
        self.auto_stories_var = BooleanVar(value=self.profile.get("auto_download_stories", False))
        self.media_interval_var = StringVar(value=str(self.profile.get("media_poll_interval_seconds", self.store.settings.get("media_poll_interval_seconds", DEFAULT_MEDIA_INTERVAL))))

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text=t("live_profile_url")).grid(row=0, column=0, sticky="w", pady=6)
        url_row = ttk.Frame(frame)
        url_row.grid(row=0, column=1, sticky="ew", pady=6)
        url_row.columnconfigure(0, weight=1)
        ttk.Entry(url_row, textvariable=self.url_var).grid(row=0, column=0, sticky="ew")
        self.resolve_button = ttk.Button(url_row, text=t("resolve"), command=self.resolve_link)
        self.resolve_button.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(frame, text=t("profile_url_media")).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.profile_url_var).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text=t("display_name")).grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.name_var).grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text=t("output_folder")).grid(row=3, column=0, sticky="w", pady=6)
        out_row = ttk.Frame(frame)
        out_row.grid(row=3, column=1, sticky="ew", pady=6)
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text=t("browse"), command=self.browse).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(frame, text=t("quality")).grid(row=4, column=0, sticky="w", pady=6)
        ttk.Combobox(frame, textvariable=self.quality_var, values=QUALITY_OPTIONS, width=12, state="readonly").grid(row=4, column=1, sticky="w", pady=6)

        ttk.Label(frame, text=t("check_every")).grid(row=5, column=0, sticky="w", pady=6)
        interval_row = ttk.Frame(frame)
        interval_row.grid(row=5, column=1, sticky="w", pady=6)
        ttk.Entry(interval_row, textvariable=self.interval_var, width=10).pack(side="left")
        ttk.Label(interval_row, text=t("seconds")).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(frame, text=t("priority"), variable=self.priority_var).grid(row=6, column=1, sticky="w", pady=6)
        ttk.Checkbutton(frame, text=t("enabled"), variable=self.enabled_var).grid(row=7, column=1, sticky="w", pady=6)
        ttk.Checkbutton(frame, text=t("record_live"), variable=self.record_live_var).grid(row=8, column=1, sticky="w", pady=6)
        ttk.Checkbutton(frame, text=t("auto_works"), variable=self.auto_videos_var).grid(row=9, column=1, sticky="w", pady=6)
        ttk.Checkbutton(frame, text=t("auto_stories"), variable=self.auto_stories_var).grid(row=10, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=t("media_check_every")).grid(row=11, column=0, sticky="w", pady=6)
        media_interval_row = ttk.Frame(frame)
        media_interval_row.grid(row=11, column=1, sticky="w", pady=6)
        ttk.Entry(media_interval_row, textvariable=self.media_interval_var, width=10).pack(side="left")
        ttk.Label(media_interval_row, text=t("seconds")).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=12, column=0, columnspan=2, sticky="e", pady=(24, 0))
        ttk.Button(actions, text=t("cancel"), command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(actions, text=t("save"), command=self.save).pack(side="right")
        self.resolve_after_id = self.after(100, self.process_resolve_results)

    def browse(self):
        path = filedialog.askdirectory(initialdir=str(ROOT_DOWNLOAD_DIR))
        if path:
            self.output_var.set(path)

    def resolve_link(self, show_errors=True):
        if self.resolving:
            return None
        url = self.url_var.get().strip()
        if "douyin.com/user/" in url:
            self.profile_url_var.set(url.split("?", 1)[0])
        platform = detect_platform(url)
        if not url or platform not in ("douyin", "youtube"):
            if show_errors:
                messagebox.showerror(t("invalid_url"), t("enter_url"))
            return None
        quality = self.quality_var.get() or self.store.settings["quality"]
        existing_name = self.name_var.get().strip()
        self.resolving = True
        self.resolve_button.configure(text=t("resolving"), state="disabled")

        def worker():
            try:
                if platform == "youtube":
                    room, stream = self.resolve_youtube(url, existing_name)
                else:
                    room, stream = asyncio.run(self.resolve_room(url, quality))
                self.resolve_result_queue.put({
                    "ok": True,
                    "room": room,
                    "stream": stream,
                    "url": url,
                    "platform": platform,
                })
            except Exception as exc:
                self.resolve_result_queue.put({"ok": False, "error": str(exc), "show_errors": show_errors})

        threading.Thread(target=worker, name="profile-link-resolver", daemon=True).start()
        return None

    def process_resolve_results(self):
        try:
            result = self.resolve_result_queue.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            self.resolving = False
            self.resolve_button.configure(text=t("resolve"), state="normal")
            if not result.get("ok"):
                if result.get("show_errors"):
                    messagebox.showerror(t("resolve_failed"), result.get("error") or t("unknown_error"), parent=self)
            else:
                room = result["room"]
                platform = result["platform"]
                url = result["url"]
                name = room.get("anchor_name") or self.name_var.get().strip() or t("platform_profile", platform=platform_label(platform))
                live_url = room.get("live_url") or url.split("?")[0]
                self.name_var.set(name)
                self.url_var.set(live_url)
                if not self.output_var.get().strip():
                    self.output_var.set(str(ROOT_DOWNLOAD_DIR / safe_name(name).rstrip(" .")))
        if self.winfo_exists():
            self.resolve_after_id = self.after(100, self.process_resolve_results)

    def destroy(self):
        after_id = getattr(self, "resolve_after_id", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            self.resolve_after_id = None
        super().destroy()

    async def resolve_room(self, url, quality=None):
        live = DouyinLiveStream()
        if "live.douyin.com/" in url:
            room = await live.fetch_web_stream_data(url)
        else:
            room = await live.fetch_app_stream_data(url)
        stream = await live.fetch_stream_url(room, quality or self.store.settings["quality"])
        return room, stream

    def resolve_youtube(self, url, name=None):
        profile = {
            "id": self.profile.get("id", ""),
            "name": name or "YouTube Profile",
            "url": url,
            "cookies": self.profile.get("cookies", ""),
            "platform": "youtube",
        }
        return MonitorEngine(self.store, queue.Queue())._resolve_youtube(profile)

    def save(self):
        url = self.url_var.get().strip()
        name = self.name_var.get().strip()
        platform = detect_platform(url)
        if not url:
            messagebox.showerror(t("missing_url"), t("enter_url_short"))
            return
        if platform not in ("douyin", "youtube"):
            messagebox.showerror(t("invalid_url"), t("enter_url_short"))
            return
        if not name:
            # Saving must never block the Tk event loop on a network request.
            # The explicit Resolve button performs that work in a background thread.
            name = fallback_name_from_url(url)
            self.name_var.set(name)
            if not self.output_var.get().strip():
                self.output_var.set(str(ROOT_DOWNLOAD_DIR / safe_name(name).rstrip(" .")))
        output_dir = self.output_var.get().strip() or str(ROOT_DOWNLOAD_DIR / safe_name(name).rstrip(" ."))
        original_profile_url = self.profile_url_var.get().strip()
        if platform == "douyin" and not original_profile_url and "douyin.com/user/" in url:
            original_profile_url = url.split("?", 1)[0]
        try:
            poll_interval = max(15, int(self.interval_var.get()))
            media_interval = max(60, int(self.media_interval_var.get()))
        except ValueError:
            messagebox.showerror(t("invalid_interval"), t("intervals_numbers"))
            return
        self.result = {
            "id": self.profile.get("id", str(uuid.uuid4())),
            "enabled": self.enabled_var.get(),
            "record_live": self.record_live_var.get(),
            "priority": self.priority_var.get(),
            "name": name,
            "url": url,
            "original_profile_url": original_profile_url if platform == "douyin" else "",
            "platform": platform,
            "fallback_live_url": url if platform == "douyin" and "live.douyin.com/" in url else self.profile.get("fallback_live_url", ""),
            "output_dir": output_dir,
            "quality": self.quality_var.get(),
            "poll_interval_seconds": poll_interval,
            "auto_download_videos": self.auto_videos_var.get() if platform == "douyin" else False,
            "auto_download_stories": self.auto_stories_var.get() if platform == "douyin" else False,
            "media_poll_interval_seconds": media_interval,
            # Douyin auth is DPAPI session files only — never persist cookie headers here.
            "cookies": "" if platform == "douyin" else (
                self.profile.get("cookies", "")
                if not _looks_like_cookie_header(self.profile.get("cookies", ""))
                else ""
            ),
            "proxy_addr": self.profile.get("proxy_addr", ""),
            "stream_orientation": self.profile.get("stream_orientation", 1),
        }
        self.destroy()


class DouyinSessionDialog(Toplevel):
    def __init__(self, parent, on_change=None):
        super().__init__(parent)
        self.on_change = on_change
        self.result_queue = queue.Queue()
        self.checking = False
        self.auto_poll = False
        self.cdp_url = ""
        self.title(t("session_title"))
        self.geometry("650x340")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close_dialog)

        self.status_var = StringVar(value=t("checking_saved"))
        self.detail_var = StringVar(value="")

        frame = ttk.Frame(self, padding=22)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text=t("session_title"), style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=t("session_help"),
            wraplength=590,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(12, 8))
        ttk.Label(
            frame,
            text=t("session_note"),
            wraplength=590,
            justify="left",
            style="Subtle.TLabel",
        ).grid(row=2, column=0, sticky="w")

        status_box = ttk.LabelFrame(frame, text=t("status"), padding=12)
        status_box.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        ttk.Label(status_box, textvariable=self.status_var, wraplength=560, justify="left").pack(anchor="w")
        ttk.Label(status_box, textvariable=self.detail_var, wraplength=560, justify="left", style="Subtle.TLabel").pack(anchor="w", pady=(5, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        ttk.Button(actions, text=t("open_login"), command=self.open_login_browser).pack(side="left")
        ttk.Button(actions, text=t("check_login"), command=self.check_now).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text=t("logout_session"), command=self.logout_session).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text=t("close"), command=self.close_dialog).pack(side="right")

        self.refresh_saved_status()
        self.after(250, self.process_results)

    def refresh_saved_status(self):
        info = saved_session_info()
        if info.get("logged_in"):
            imported_at = info.get("imported_at") or t("unknown_time")
            self.status_var.set(t("session_available"))
            self.detail_var.set(t("session_imported", imported_at=imported_at, count=info.get("cookie_count", 0)))
        else:
            self.status_var.set(t("session_missing"))
            self.detail_var.set(t("session_scan_hint"))

    def open_login_browser(self):
        try:
            launched = launch_douyin_login_browser()
        except Exception as exc:
            logging.exception("Could not open Douyin login browser")
            messagebox.showerror(t("login_browser_failed"), str(exc), parent=self)
            return
        self.cdp_url = launched["cdp_url"]
        self.auto_poll = True
        self.status_var.set(t("waiting_qr"))
        self.detail_var.set(t("scan_qr"))
        self.after(1200, self.check_now)

    def check_now(self):
        if self.checking:
            return
        self.checking = True
        urls = [self.cdp_url] if self.cdp_url else [DEFAULT_CHROME_CDP, "http://127.0.0.1:9223"]

        def worker():
            last_error = "No compatible logged-in Chrome session was found"
            for url in urls:
                if not url:
                    continue
                try:
                    info = import_chrome_session(url)
                    self.result_queue.put({"ok": True, "info": info})
                    return
                except Exception as exc:
                    last_error = str(exc)
            self.result_queue.put({"ok": False, "error": last_error})

        threading.Thread(target=worker, name="douyin-session-import", daemon=True).start()

    def process_results(self):
        try:
            result = self.result_queue.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            self.checking = False
            if result.get("ok"):
                self.auto_poll = False
                self.cdp_url = ""
                info = result["info"]
                self.status_var.set(t("login_imported"))
                self.detail_var.set(t("saved_cookies", count=info.get("cookie_count", 0)))
                if self.on_change:
                    self.on_change()
            else:
                self.status_var.set(t("waiting_logged_in"))
                self.detail_var.set(result.get("error") or t("login_not_ready"))
                if self.auto_poll:
                    self.after(1800, self.check_now)
        if self.winfo_exists():
            self.after(250, self.process_results)

    def logout_session(self):
        try:
            clear_saved_session()
        except Exception as exc:
            logging.exception("Could not clear saved Douyin session")
            messagebox.showerror(t("session_clear_failed"), str(exc), parent=self)
            return
        self.refresh_saved_status()
        if self.on_change:
            self.on_change()
        messagebox.showinfo(t("session_title"), t("session_cleared"), parent=self)

    def close_dialog(self):
        self.auto_poll = False
        if self.cdp_url:
            try:
                close_cdp_browser(self.cdp_url)
            except Exception:
                logging.debug("Could not close login browser on dialog close", exc_info=True)
            self.cdp_url = ""
        self.destroy()


class SettingsDialog(Toplevel):
    def __init__(self, parent, store):
        super().__init__(parent)
        self.store = store
        self.title(t("settings_title"))
        self.geometry("560x470")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.ffmpeg_var = StringVar(value=store.settings["ffmpeg_path"])
        self.ytdlp_var = StringVar(value=store.settings.get("ytdlp_path", str(DEFAULT_YTDLP_PATH)))
        self.poll_var = StringVar(value=str(store.settings.get("new_profile_poll_interval_seconds", DEFAULT_NEW_PROFILE_INTERVAL)))
        self.container_var = StringVar(value=store.settings["container"])
        self.hidden_var = BooleanVar(value=store.settings.get("start_hidden_to_tray", False))
        autostart_value = store.settings.get("start_with_windows")
        if autostart_value is None:
            autostart_value = is_autostart_enabled()
        self.autostart_var = BooleanVar(value=autostart_value)
        self.priority_risk_backoff_var = StringVar(value=str(store.settings["priority_risk_control_backoff_seconds"]))
        self.standard_risk_backoff_var = StringVar(value=str(store.settings["standard_risk_control_backoff_seconds"]))
        self.stall_timeout_var = StringVar(value=str(store.settings.get("recording_stall_timeout_seconds", 300)))
        self.language_labels = {code: label for code, label in LANGUAGE_CHOICES}
        self.language_codes = {label: code for code, label in LANGUAGE_CHOICES}
        initial_language = store.settings.get("language") or "zh-CN"
        self.language_var = StringVar(value=self.language_labels.get(initial_language, "简体中文"))

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text=t("language")).grid(row=0, column=0, sticky="w", pady=6)
        ttk.Combobox(
            frame,
            textvariable=self.language_var,
            values=[label for _code, label in LANGUAGE_CHOICES],
            width=16,
            state="readonly",
        ).grid(row=0, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=t("ffmpeg_path")).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.ffmpeg_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text=t("ytdlp_path")).grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.ytdlp_var).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text=t("default_interval")).grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.poll_var, width=12).grid(row=3, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=t("container")).grid(row=4, column=0, sticky="w", pady=6)
        ttk.Combobox(frame, textvariable=self.container_var, values=["mkv"], width=12, state="readonly").grid(row=4, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=t("priority_cooldown")).grid(row=5, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.priority_risk_backoff_var, width=12).grid(row=5, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=t("normal_cooldown")).grid(row=6, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.standard_risk_backoff_var, width=12).grid(row=6, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=t("stall_timeout")).grid(row=7, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.stall_timeout_var, width=12).grid(row=7, column=1, sticky="w", pady=6)
        ttk.Checkbutton(frame, text=t("start_with_windows"), variable=self.autostart_var).grid(row=8, column=1, sticky="w", pady=6)
        ttk.Checkbutton(frame, text=t("start_hidden"), variable=self.hidden_var).grid(row=9, column=1, sticky="w", pady=6)

        actions = ttk.Frame(frame)
        actions.grid(row=10, column=0, columnspan=2, sticky="e", pady=(24, 0))
        ttk.Button(actions, text=t("cancel"), command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(actions, text=t("save"), command=self.save).pack(side="right")

    def save(self):
        try:
            poll = max(15, int(self.poll_var.get()))
            priority_risk_backoff = max(1, int(self.priority_risk_backoff_var.get()))
            standard_risk_backoff = max(1, int(self.standard_risk_backoff_var.get()))
            stall_timeout = max(60, int(self.stall_timeout_var.get()))
        except ValueError:
            messagebox.showerror(t("invalid_settings"), t("settings_numbers"))
            return
        try:
            updated_settings = dict(self.store.settings)
            previous_language = updated_settings.get("language") or "zh-CN"
            language_code = self.language_codes.get(self.language_var.get(), "zh-CN")
            updated_settings["ffmpeg_path"] = resolve_ffmpeg_executable(self.ffmpeg_var.get().strip())
            updated_settings["ytdlp_path"] = resolve_ytdlp_executable(self.ytdlp_var.get().strip())
            updated_settings["new_profile_poll_interval_seconds"] = poll
            updated_settings["container"] = "mkv"
            updated_settings["start_hidden_to_tray"] = self.hidden_var.get()
            updated_settings["start_with_windows"] = bool(self.autostart_var.get())
            updated_settings["priority_risk_control_backoff_seconds"] = priority_risk_backoff
            updated_settings["standard_risk_control_backoff_seconds"] = standard_risk_backoff
            updated_settings["recording_stall_timeout_seconds"] = stall_timeout
            updated_settings["language"] = language_code
            if updated_settings["start_with_windows"] != is_autostart_enabled():
                set_autostart_enabled(updated_settings["start_with_windows"])
            self.store.settings = updated_settings
            self.store.save()
            set_language(language_code)
        except Exception as exc:
            logging.exception("Settings save failed")
            messagebox.showerror(t("settings_save_failed"), str(exc))
            return
        if language_code != previous_language:
            messagebox.showinfo(t("settings_title"), t("language_restart"), parent=self)
        self.destroy()


class SingleVideoDialog(Toplevel):
    """Download one Douyin video from a pasted share link or video URL."""

    def __init__(self, parent, event_queue, notify_callback=None):
        super().__init__(parent)
        self.event_queue = event_queue
        self.notify_callback = notify_callback
        self.title(t("single_title"))
        self.geometry("700x320")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.worker = None
        self._status_text = t("single_hint")
        self._result = None
        self._output_dir = str(ROOT_DOWNLOAD_DIR / t("folder_single_videos"))

        self.url_var = StringVar()
        self.output_var = StringVar(value=self._output_dir)
        self.status_var = StringVar(value=self._status_text)

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text=t("video_link")).grid(row=0, column=0, sticky="w", pady=6)
        url_entry = ttk.Entry(frame, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)
        url_entry.focus_set()
        ttk.Label(frame, text=t("save_to")).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text=t("browse"), command=self.browse_output).grid(row=1, column=2, padx=(8, 0), pady=6)

        self.status_label = ttk.Label(
            frame, textvariable=self.status_var, style="Subtle.TLabel", wraplength=620, justify="left"
        )
        self.status_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=3, sticky="e", pady=(24, 0))
        ttk.Button(actions, text=t("close"), command=self.destroy).pack(side="right", padx=(8, 0))
        self.open_folder_button = ttk.Button(actions, text=t("open_folder"), command=self.open_folder, state="disabled")
        self.open_folder_button.pack(side="right", padx=(8, 0))
        self.download_button = ttk.Button(actions, text=t("download"), command=self.start_download)
        self.download_button.pack(side="right")

        self.bind("<Return>", lambda event: self.start_download())

    def browse_output(self):
        initial = self.output_var.get().strip() or str(ROOT_DOWNLOAD_DIR)
        path = filedialog.askdirectory(initialdir=initial, parent=self)
        if path:
            self.output_var.set(path)

    def start_download(self):
        if self.worker is not None and self.worker.is_alive():
            return
        link = self.url_var.get().strip()
        if not link:
            messagebox.showinfo(t("missing_link"), t("paste_link"), parent=self)
            return
        self._output_dir = self.output_var.get().strip() or str(ROOT_DOWNLOAD_DIR / t("folder_single_videos"))
        self._result = None
        self.download_button.config(state="disabled")
        self.open_folder_button.config(state="disabled")
        self._status_text = t("resolving_link")
        self.status_var.set(self._status_text)
        self.worker = threading.Thread(
            target=self._run_download, args=(link, self._output_dir), name="single-video-download", daemon=True
        )
        self.worker.start()
        self.after(300, self._poll_progress)

    def _on_progress(self, progress):
        """Progress callback (runs on the download worker thread)."""
        phase = progress.get("phase") or "working"
        if phase == "downloading":
            done = progress.get("bytes_downloaded")
            total = progress.get("bytes_total") or 0
            if isinstance(done, int) and total:
                percent = min(100, done * 100 // max(total, 1))
                message = t(
                    "downloading_progress",
                    done=byte_size_text(done),
                    total=byte_size_text(total),
                    percent=percent,
                )
            else:
                message = t("downloading_video")
        elif phase == "retrying":
            message = t("retrying_download", error=progress.get("error") or "network error")
        else:
            message = t("working_phase", phase=phase)
        self._status_text = message
        try:
            self.event_queue.put(
                {"profile_id": "engine", "message": t("single_prefix", message=message), "state": {}, "time": now_text()}
            )
        except Exception:
            pass

    def _run_download(self, link, output_dir):
        try:
            result = download_video_by_url(link, output_dir, progress_callback=self._on_progress)
        except Exception as exc:
            logging.exception("Single video download crashed")
            result = {
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "files": [],
                "title": "",
                "author": "",
                "output_dir": output_dir,
            }
        self._result = result

    def _poll_progress(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._result is not None:
            self._finish()
            return
        self.status_var.set(self._status_text)
        self.after(300, self._poll_progress)

    def _finish(self):
        result = self._result or {}
        status = result.get("status")
        title = result.get("title") or "video"
        self.download_button.config(state="normal")
        if status == "ok":
            files = result.get("files") or []
            names = ", ".join(Path(item).name for item in files) or t("file_saved")
            self.status_var.set(t("saved_title", title=title, names=names))
            self.open_folder_button.config(state="normal")
            self._output_dir = result.get("output_dir") or self._output_dir
            summary = t("single_saved", title=title)
        elif status == "skipped":
            self.status_var.set(t("already_downloaded", title=title))
            self.open_folder_button.config(state="normal")
            self._output_dir = result.get("output_dir") or self._output_dir
            summary = t("single_already", title=title)
        else:
            self.status_var.set(t("failed", message=result.get("message") or t("unknown_error")))
            summary = t("single_failed")
        try:
            self.event_queue.put({"profile_id": "engine", "message": summary, "state": {}, "time": now_text()})
        except Exception:
            pass
        if self.notify_callback:
            try:
                self.notify_callback(summary)
            except Exception:
                pass

    def open_folder(self):
        try:
            Path(self._output_dir).mkdir(parents=True, exist_ok=True)
            os.startfile(self._output_dir)
        except Exception as exc:
            messagebox.showerror(t("open_folder_failed"), str(exc), parent=self)



class RecorderApp:
    def __init__(self):
        setup_logging()
        self.store = RecorderStore()
        self.store.save()
        self.queue = queue.Queue()
        self.engine = MonitorEngine(self.store, self.queue)
        self.media_engine = MediaDownloadEngine(self.store, self.queue, notify_callback=self._tray_notify)
        self.rows = {}
        self.tray_icon = None

        self.root = Tk()
        install_exception_hooks(self.root)
        self.root.title(t("app_title"))
        self.root.geometry("1180x700")
        self.root.minsize(1040, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._setup_style()
        self._build_ui()
        self._start_tray()
        self.refresh_profiles()
        self.root.after(300, self.process_events)
        self.root.after(1000, self.check_show_signal)
        if self.store.settings.get("start_hidden_to_tray", False):
            self.root.after(100, self.hide_to_tray)

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Subtle.TLabel", foreground="#5f6b7a")
        style.configure("Toolbar.TFrame", background="#f5f7fa")
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def _build_ui(self):
        shell = ttk.Frame(self.root, padding=18)
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(3, weight=1)
        shell.columnconfigure(0, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=t("app_title"), style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(header, text=t("ready"), style="Subtle.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.session_status_var = StringVar(value=t("session_checking"))
        ttk.Label(header, textvariable=self.session_status_var, style="Subtle.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.refresh_session_status()

        toolbar = ttk.Frame(shell)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(16, 8))
        ttk.Button(toolbar, text=t("start_monitoring"), command=self.start_monitoring).pack(side="left")
        ttk.Button(toolbar, text=t("stop"), command=self.stop_monitoring).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text=t("refresh_now"), command=self.refresh_now).pack(side="left", padx=(8, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(toolbar, text=t("download_media"), command=self.download_media_now).pack(side="left")
        ttk.Button(toolbar, text=t("download_video"), command=self.download_single_video).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text=t("douyin_login"), command=self.open_session_login).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text=t("settings"), command=self.open_settings).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text=t("hide"), command=self.hide_to_tray).pack(side="right")

        profile_toolbar = ttk.Frame(shell)
        profile_toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ttk.Button(profile_toolbar, text=t("add_profile"), command=self.add_profile).pack(side="left")
        ttk.Button(profile_toolbar, text=t("edit"), command=self.edit_profile).pack(side="left", padx=(8, 0))
        ttk.Button(profile_toolbar, text=t("remove"), command=self.remove_profile).pack(side="left", padx=(8, 0))
        ttk.Separator(profile_toolbar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(profile_toolbar, text=t("open_folder"), command=self.open_folder).pack(side="left")

        columns = PROFILE_TABLE_COLUMNS
        headings = {
            "enabled": t("col_on"),
            "name": t("col_profile"),
            "status": t("col_live"),
            "media_auto": t("col_media_auto"),
            "media_progress": t("col_media_progress"),
            "next_check": t("col_next_check"),
        }
        widths = {
            "enabled": 44,
            "name": 165,
            "status": 170,
            "media_auto": 105,
            "media_progress": 260,
            "next_check": 115,
        }
        table = ttk.Frame(shell)
        table.grid(row=3, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=widths[column] if column in {"enabled", "media_auto", "next_check"} else 100,
                anchor="w",
                stretch=column in {"name", "status", "media_progress"},
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        y_scroll.grid(row=0, column=1, sticky="ns")

        log_frame = ttk.LabelFrame(shell, text=t("activity"))
        log_frame.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.activity = ttk.Label(log_frame, text=t("no_activity"), anchor="w")
        self.activity.pack(fill="x", padx=10, pady=10)

    def _start_tray(self):
        if self.tray_icon:
            return
        try:
            image = Image.new("RGB", (64, 64), "#101827")
            draw = ImageDraw.Draw(image)
            draw.ellipse((14, 14, 50, 50), fill="#e11d48")
            draw.polygon([(29, 24), (29, 40), (43, 32)], fill="white")
            menu = pystray.Menu(
                pystray.MenuItem(t("tray_show"), lambda: self.root.after(0, self.show_window), default=True),
                pystray.MenuItem(t("tray_start"), lambda: self.root.after(0, self.start_monitoring)),
                pystray.MenuItem(t("tray_stop"), lambda: self.root.after(0, self.stop_monitoring)),
                pystray.MenuItem(t("tray_refresh"), lambda: self.root.after(0, self.refresh_now)),
                pystray.MenuItem(t("tray_open_folder"), lambda: self.root.after(0, self.open_root_folder)),
                pystray.MenuItem(t("tray_exit"), lambda: self.root.after(0, self.exit_app)),
            )
            self.tray_icon = pystray.Icon("live-recorder", image, t("app_title"), menu)

            def setup_icon(icon):
                try:
                    icon.visible = True
                    logging.info("Tray icon visible.")
                except Exception:
                    logging.exception("Failed to show tray icon")

            self.tray_icon.run_detached(setup=setup_icon)
            logging.info("Tray icon started.")
            self.root.after(10000, self.refresh_tray_icon)
        except Exception:
            self.tray_icon = None
            logging.exception("Failed to start tray icon")
            self.root.after(10000, self._start_tray)

    def refresh_tray_icon(self):
        try:
            if not self.tray_icon:
                self._start_tray()
                return
            self.tray_icon.visible = False
            self.tray_icon.visible = True
            logging.info("Tray icon refreshed.")
        except Exception:
            logging.exception("Failed to refresh tray icon")
            try:
                if self.tray_icon:
                    self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
            self.root.after(10000, self._start_tray)
            return
        self.root.after(300000, self.refresh_tray_icon)

    def selected_profile(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return self.store.get_profile(selection[0])

    def refresh_profiles(self):
        existing = set(self.tree.get_children())
        for profile in self.store.profiles:
            row = self.rows.get(profile["id"], {})
            media_modes = []
            if profile.get("auto_download_videos"):
                media_modes.append(t("works"))
            if profile.get("auto_download_stories"):
                media_modes.append(t("stories"))
            live_status = row.get("status", t("ready"))
            live_status = {
                "Recording": t("recording"),
                "Ready": t("ready"),
                "Live": t("live"),
                "Offline": t("offline"),
                "Disabled": t("disabled"),
                "Live off": t("live_recording_off"),
                "Error": t("error"),
                "Captcha": t("captcha"),
                "Rate limited": t("rate_limited"),
                "Not visible": t("not_visible"),
                "Unsupported stream": t("unsupported_stream"),
                "Empty response": t("empty_response"),
            }.get(live_status, live_status)
            if row.get("recording"):
                recording_details = [t("recording")]
                if row.get("elapsed"):
                    recording_details.append(row["elapsed"])
                if row.get("file_size"):
                    recording_details.append(row["file_size"])
                live_status = " • ".join(recording_details)
            if not profile.get("record_live", True) and not row.get("recording"):
                live_status = t("live_recording_off")
            profile_name = f"★ {profile['name']}" if profile.get("priority") else profile["name"]
            media_auto = " + ".join(media_modes) if media_modes else t("off")
            media_progress = row.get("media_progress") or row.get("media_status")
            if not media_progress:
                media_progress = t("waiting") if media_modes else "—"
            next_check = row.get("next_check", "")
            if not profile.get("record_live", True):
                next_check = row.get("media_next_check") or next_check
            values = (
                t("yes") if profile.get("enabled", True) else t("no"),
                profile_name,
                live_status,
                media_auto,
                media_progress,
                next_check,
            )
            if profile["id"] in existing:
                self.tree.item(profile["id"], values=values)
                existing.remove(profile["id"])
            else:
                self.tree.insert("", "end", iid=profile["id"], values=values)
        for item in existing:
            self.tree.delete(item)

    def process_events(self):
        while True:
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            profile_id = event["profile_id"]
            if profile_id != "engine":
                row = self.rows.setdefault(profile_id, {})
                row.update(event.get("state", {}))
            self.activity.config(text=f"{event['time']}  {event['message']}")
            self.status_label.config(text=event["message"])
            logging.info("%s: %s", profile_id, event["message"])
        self.refresh_profiles()
        self.root.after(500, self.process_events)

    def check_show_signal(self):
        if SHOW_SIGNAL_FILE.exists():
            SHOW_SIGNAL_FILE.unlink(missing_ok=True)
            self.show_window()
        self.root.after(1000, self.check_show_signal)

    def start_monitoring(self):
        self.engine.start()
        self.media_engine.start()

    def refresh_now(self):
        self.engine.refresh_all()
        self.media_engine.refresh_all()

    def stop_monitoring(self):
        if any(p.poll() is None for p in list(self.engine.processes.values())):  # FIX-L15: snapshot to avoid RuntimeError
            if not messagebox.askyesno(t("stop_recordings_title"), t("stop_recordings_body")):
                return
        self.media_engine.stop()
        self.engine.stop(terminate_recordings=True)

    def add_profile(self):
        dialog = ProfileDialog(self.root, self.store)
        self.root.wait_window(dialog)
        if dialog.result:
            self.store.upsert_profile(dialog.result)
            Path(dialog.result["output_dir"]).mkdir(parents=True, exist_ok=True)
            self.engine.wake_event.set()
            self.media_engine.wake_event.set()
            self.refresh_profiles()

    def edit_profile(self):
        profile = self.selected_profile()
        if not profile:
            messagebox.showinfo(t("select_profile"), t("select_profile_edit"))
            return
        dialog = ProfileDialog(self.root, self.store, profile)
        self.root.wait_window(dialog)
        if dialog.result:
            self.store.upsert_profile(dialog.result)
            Path(dialog.result["output_dir"]).mkdir(parents=True, exist_ok=True)
            if not wants_live_recording(dialog.result):
                self.engine.stop_profile_recording(dialog.result["id"], t("live_recording_off_detail"))
            self.engine.wake_event.set()
            self.media_engine.wake_event.set()
            self.refresh_profiles()

    def remove_profile(self):
        profile = self.selected_profile()
        if not profile:
            messagebox.showinfo(t("select_profile"), t("select_profile_remove"))
            return
        if self.engine._is_recording(profile["id"]):
            messagebox.showerror(t("recording_active"), t("stop_before_remove"))
            return
        if messagebox.askyesno(t("remove_profile_title"), t("remove_profile_body", name=profile["name"])):
            self.store.remove_profile(profile["id"])
            self.rows.pop(profile["id"], None)
            self.refresh_profiles()

    def open_folder(self):
        profile = self.selected_profile()
        if profile:
            path = profile["output_dir"]
        else:
            path = str(ROOT_DOWNLOAD_DIR)
        os.startfile(path)

    def open_root_folder(self):
        os.startfile(str(ROOT_DOWNLOAD_DIR))

    def refresh_session_status(self):
        info = saved_session_info()
        if info.get("logged_in"):
            imported_at = info.get("imported_at") or t("unknown_time")
            self.session_status_var.set(t("session_saved", imported_at=imported_at))
        else:
            self.session_status_var.set(t("session_optional"))

    def open_session_login(self):
        dialog = DouyinSessionDialog(self.root, on_change=self.refresh_session_status)
        self.root.wait_window(dialog)
        self.refresh_session_status()

    def download_media_now(self):
        profile = self.selected_profile()
        if not profile:
            messagebox.showinfo(t("select_profile"), t("select_profile_media"))
            return
        if profile.get("platform") != "douyin" and detect_platform(profile.get("url", "")) != "douyin":
            messagebox.showinfo(t("douyin_only"), t("media_douyin_only"))
            return
        if not profile.get("auto_download_videos"):
            messagebox.showinfo(
                t("media_disabled"),
                t("media_disabled_body"),
            )
            return
        self.media_engine.refresh_profile(profile["id"])

    def download_single_video(self):
        existing = getattr(self, "_single_video_dialog", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
        self._single_video_dialog = SingleVideoDialog(
            self.root, event_queue=self.queue, notify_callback=self._tray_notify
        )

    def open_settings(self):
        dialog = SettingsDialog(self.root, self.store)
        self.root.wait_window(dialog)

    def _tray_notify(self, message):
        """Send a Windows toast notification via the tray icon (best-effort)."""
        if self.tray_icon:
            try:
                self.tray_icon.notify(message, t("app_title"))
            except Exception:
                pass

    def hide_to_tray(self):
        # X / Hide: keep process + recordings alive; tray icon remains the reopen path.
        try:
            self.root.withdraw()
            if self.tray_icon:
                try:
                    self.tray_icon.visible = True
                    self.tray_icon.notify(
                        t("tray_hidden"),
                        t("app_title"),
                    )
                except Exception:
                    # notify() is best-effort; some Windows builds reject balloon tips.
                    pass
            logging.info("Main window hidden to tray (process still running).")
        except Exception:
            logging.exception("Could not hide main window")

    def show_window(self):
        # Force a real restore path so taskbar clicks and second-launch signals
        # can recover a withdrawn/stuck-on-taskbar Tk window.
        try:
            self.root.deiconify()
            self.root.state("normal")
            # Always re-anchor on restore so multi-monitor disconnects cannot leave
            # the window permanently off-screen.
            self.root.geometry("1200x720+120+60")
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            logging.info("Main window restored.")
        except Exception:
            logging.exception("Could not restore main window")

    def exit_app(self):
        if any(p.poll() is None for p in list(self.engine.processes.values())):  # FIX-L15: snapshot to avoid RuntimeError
            if not messagebox.askyesno(t("exit_title"), t("exit_body")):
                return
        logging.info("Exit requested; stopping engines.")
        self.media_engine.stop()
        self.engine.stop(terminate_recordings=True)
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                logging.exception("Could not stop tray icon")
            self.tray_icon = None
        release_app_lock()
        try:
            self.root.destroy()
        except Exception:
            logging.exception("Could not destroy main window")

    def run(self):
        try:
            self.root.mainloop()
        finally:
            logging.info("Mainloop ended.")
            release_app_lock()


def run_check():
    setup_logging(console=True)
    install_exception_hooks()
    store = RecorderStore()
    events = queue.Queue()
    engine = MonitorEngine(store, events)
    for profile in store.profiles:
        if wants_live_recording(profile):
            engine._check_profile(profile)
    while not events.empty():
        event = events.get()
        print_console(f"{event['time']} {event['profile_id']} {event['message']} {event.get('state', {})}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Run one profile check without opening the UI.")
    args = parser.parse_args()
    if args.check:
        run_check()
        return 0
    setup_logging()
    install_exception_hooks()
    acquired = acquire_app_lock()
    if not acquired:
        return 0
    try:
        app = RecorderApp()
        app.start_monitoring()
        app.run()
        return 0
    except Exception:
        logging.exception("Application crashed")
        raise
    finally:
        release_app_lock()


if __name__ == "__main__":
    raise SystemExit(main())


