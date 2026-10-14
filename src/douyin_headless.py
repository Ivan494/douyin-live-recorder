"""Linux service for the recorder's supervised live and story engines."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import queue
import re
import shutil
import signal
import threading
import time

# This must precede the application import: servers do not install Tk/pystray.
os.environ["DOUYIN_HEADLESS"] = "1"
import douyin_recorder_app as app

GIB = 1024 ** 3


def redact(value):
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()
                if not any(secret in str(key).lower() for secret in ("cookie", "authorization", "token", "headers"))}
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = app.redact_sensitive_text(value)
        text = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", text)
        text = re.sub(r"(?i)(cookie|authorization)\s*[:=][^\r\n]+", r"\1=[redacted]", text)
        return re.sub(r"(?i)\b(sessionid(?:_ss)?|sid_guard|uid_tt|sid_tt|msToken|access_token)=([^;\s]+)",
                      r"\1=[redacted]", text)
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging():
    app.setup_logging(console=True)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def normalize_output_path(value, name, output_root):
    """Translate imported Windows paths and keep captures below the output root."""
    root = Path(output_root).expanduser().resolve()
    raw = str(value or "").strip()
    if os.name != "nt" and PureWindowsPath(raw).drive:
        return str(root / app.safe_name(name))
    path = Path(app.expand_portable_path(raw).replace("\\", "/")) if raw else root / app.safe_name(name)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path == root or root not in path.parents:
        return str(root / app.safe_name(name))
    return str(path)


def prepare_store(store):
    root = store.settings.get("headless_output_root") or str(app.APP_DIR.parent / "downloads")
    root = Path(root).expanduser().resolve()
    store.settings["headless_output_root"] = str(root)
    # Enforce the requested scope even after a later config edit.
    for profile in store.profiles:
        profile["auto_download_videos"] = False
        profile["output_dir"] = normalize_output_path(profile.get("output_dir"), profile.get("name"), root)
    return store


def validate(store):
    """Offline configuration validation; does not resolve or probe any stream."""
    ids = [profile["id"] for profile in store.profiles]
    if len(set(ids)) != len(ids):
        raise ValueError("Profile IDs must be unique")
    app.resolve_ffmpeg_executable(store.settings["ffmpeg_path"])
    app.resolve_trusted_executable(
        store.settings.get("ffprobe_path") or str(app.DEFAULT_FFPROBE_PATH),
        allowed_basenames={"ffprobe.exe"} if os.name == "nt" else {"ffprobe"},
        trusted_roots=app._trusted_tool_roots(),
    )
    minimum = float(store.settings.get("headless_min_free_gb", 5))
    critical = float(store.settings.get("headless_critical_free_gb", 1))
    if not 0 < critical < minimum:
        raise ValueError("Disk thresholds must satisfy 0 < critical < minimum")
    for profile in store.profiles:
        if not profile.get("url") or app.detect_platform(profile["url"]) != "douyin":
            raise ValueError(f"Unsupported profile URL for {profile.get('name')}")
        if profile.get("auto_download_videos"):
            raise ValueError("Post downloads must be disabled")
    return {
        "profiles": len(ids),
        "enabled_profiles": sum(bool(p.get("enabled", True)) for p in store.profiles),
        "live_profiles": sum(app.wants_live_recording(p) for p in store.profiles),
        "story_profiles": sum(bool(p.get("enabled", True)) and bool(p.get("auto_download_stories")) for p in store.profiles),
        "post_downloads": False,
        "output_root": store.settings["headless_output_root"],
        "min_free_gb": minimum,
        "critical_free_gb": critical,
    }


class InstanceLock:
    """Keep the lock descriptor open; flock releases it even after a crash."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(" ")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(str(os.getpid()) + "\n")
            self.handle.flush()
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another headless recorder is already running") from exc
        return self

    def __exit__(self, *_args):
        if self.handle:
            self.handle.close()
            self.handle = None


class DiskGuard:
    def __init__(self, store, usage=shutil.disk_usage):
        self.store = store
        self.usage = usage
        self.minimum = int(float(store.settings.get("headless_min_free_gb", 5)) * GIB)
        self.critical = int(float(store.settings.get("headless_critical_free_gb", 1)) * GIB)

    def free(self, path):
        directory = Path(path).resolve()
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        try:
            return self.usage(directory).free
        except OSError:
            logging.exception("Unable to check available recording storage")
            return 0  # Fail closed: never fill storage when the check fails.

    def can_start(self, profile):
        return self.free(profile["output_dir"]) >= self.minimum

    def snapshot(self):
        roots = {p["output_dir"] for p in self.store.profiles if p.get("enabled", True)}
        free = min((self.free(path) for path in roots), default=self.free(self.store.settings["headless_output_root"]))
        return {"free_bytes": free, "new_captures_paused": free < self.minimum,
                "critically_low": free < self.critical}


class HeadlessMonitorEngine(app.MonitorEngine):
    def __init__(self, store, events, disk):
        super().__init__(store, events)
        self.disk = disk
        self.deferred_finalizers = {}
        self.deferred_lock = threading.Lock()
        # Remux creates a second copy of all raw parts. Serialize the disk
        # decision and the entire copy so finalizers cannot spend the same space.
        self.finalize_lock = threading.Lock()

    def recover_pending_recording_sessions(self):
        # Disk-deferred sessions must be finalized separately, never resumed into
        # an unrelated new broadcast. This also recovers them after restart.
        for profile in self.store.profiles:
            root = Path(profile["output_dir"]) / ".recording_sessions"
            for manifest_path in root.glob("*/session.json"):
                manifest = self._read_recording_manifest(manifest_path)
                if not manifest or manifest.get("status") not in {"waiting_for_disk", "finalizing"} or manifest.get("profile_id") != profile["id"]:
                    continue
                manifest["manifest_path"] = str(manifest_path)
                manifest["session_dir"] = str(manifest_path.parent)
                with self.deferred_lock:
                    self.deferred_finalizers[str(manifest_path.parent)] = (manifest, manifest.get("stop_reason", "Disk space recovered"))
        return super().recover_pending_recording_sessions()

    def _check_profile(self, profile):
        if not self.disk.can_start(profile):
            self.emit(profile["id"], "New capture paused: low free disk space.", status="Disk paused")
            return
        return super()._check_profile(profile)

    def _start_recording(self, profile, stream):
        # Recheck just before Popen; an in-flight network check may outlast the guard.
        if not self.disk.can_start(profile):
            self.emit(profile["id"], "New capture paused: low free disk space.", status="Disk paused")
            return
        return super()._start_recording(profile, stream)

    def _finalize_recording_worker(self, recording, reason):
        with self.finalize_lock:
            return self._finalize_with_disk_guard(recording, reason)

    def _finalize_with_disk_guard(self, recording, reason):
        needed = self._parts_size(recording) + self.disk.critical
        if self.disk.free(recording["session_dir"]) < needed:
            recording["status"] = "waiting_for_disk"
            recording["stop_reason"] = reason
            self._save_recording_manifest(recording)
            with self.deferred_lock:
                self.deferred_finalizers[recording["session_dir"]] = (recording, reason)
            self.emit(recording["profile_id"], "Finalization deferred until disk space recovers; raw MKV parts preserved.",
                      status="Waiting for disk", recording=False, current_file=recording["session_dir"])
            return
        return super()._finalize_recording_worker(recording, reason)

    def retry_deferred_finalizers(self):
        if any(thread.is_alive() for thread in self.finalizer_threads):
            return
        with self.deferred_lock:
            ready = [(key, item) for key, item in self.deferred_finalizers.items()
                     if self.disk.free(item[0]["session_dir"]) >= self._parts_size(item[0]) + self.disk.critical]
            for key, (recording, reason) in ready[:1]:
                del self.deferred_finalizers[key]
                thread = threading.Thread(target=self._finalize_recording_worker, args=(recording, reason),
                                          name=f"recording-finalizer-{recording['profile_id']}", daemon=False)
                self.finalizer_threads.append(thread)
                thread.start()


class HeadlessStoryEngine(app.MediaDownloadEngine):
    def __init__(self, store, events, disk):
        super().__init__(store, events)
        self.disk = disk

    @staticmethod
    def enabled_for_profile(profile):
        return bool(profile.get("auto_download_stories"))

    def _check_profile(self, profile):
        if not self.disk.can_start(profile):
            self.emit(profile["id"], "New stories paused: low free disk space.", media_status="Disk paused")
            return
        # Keep identity equal to the stored profile for ProfileCancellation.
        profile["auto_download_videos"] = False
        return super()._check_profile(profile)


def atomic_status(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(redact(data), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class HeadlessService:
    def __init__(self, store, status_path=None):
        self.store = prepare_store(store)
        self.events = app.RecorderEventQueue()
        self.disk = DiskGuard(store)
        self.live = HeadlessMonitorEngine(store, self.events, self.disk)
        self.stories = HeadlessStoryEngine(store, self.events, self.disk)
        self.stop_event = threading.Event()
        self.status_path = Path(status_path or app.APP_DIR / "status.json")
        self.started_at = app.now_text()
        self.states = {}
        self.disk_state = {}
        self.stories_disk_stopped = False

    def request_stop(self, *_args):
        self.stop_event.set()

    def drain_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            event = redact(event)
            profile_id = event["profile_id"]
            state = self.states.setdefault(profile_id, {})
            state.update(event.get("state") or {})
            state["last_event"] = event.get("message", "")
            state["updated_at"] = event.get("time", "")
            logging.info("%s: %s", profile_id, event.get("message", ""))

    def write_status(self, phase):
        atomic_status(self.status_path, {
            "phase": phase, "pid": os.getpid(), "started_at": self.started_at,
            "updated_at": app.now_text(), "post_downloads": False,
            "disk": self.disk_state,
            "engines": {"live_running": self.live.is_running(), "stories_running": self.stories.is_running(),
                        "active_recordings": len(self.live.processes),
                        "active_finalizers": sum(t.is_alive() for t in self.live.finalizer_threads),
                        "deferred_finalizers": len(self.live.deferred_finalizers)},
            "profiles": [{"id": p["id"], "name": p.get("name", ""), "enabled": p.get("enabled", True),
                          "record_live": p.get("record_live", True), "stories": p.get("auto_download_stories", False),
                          "state": self.states.get(p["id"], {})} for p in self.store.profiles],
        })

    def enforce_disk_guard(self):
        previous = self.disk_state
        self.disk_state = self.disk.snapshot()
        if previous.get("new_captures_paused") != self.disk_state["new_captures_paused"]:
            logging.warning("Disk guard: new captures %s (%.2f GiB free)",
                            "paused" if self.disk_state["new_captures_paused"] else "resumed",
                            self.disk_state["free_bytes"] / GIB)
        if self.disk_state["critically_low"]:
            # Stop writing before the filesystem fills; the live engine preserves MKV parts.
            for profile_id in list(self.live.processes):
                self.live.stop_profile_recording(profile_id, "Critical low disk space")
            if self.stories.is_running():
                self.stories.stop()
                self.stories_disk_stopped = True
        elif not self.disk_state["new_captures_paused"] and self.stories_disk_stopped and not self.stories.is_running():
            self.stories.start()
            self.stories_disk_stopped = False
        self.live.retry_deferred_finalizers()

    def shutdown(self):
        self.stories.stop()
        self.live.stop(terminate_recordings=True)
        deadline = time.monotonic() + max(30, float(self.store.settings.get("headless_shutdown_timeout_seconds", 300)))
        while time.monotonic() < deadline:
            workers = [thread for thread in (self.live.thread, self.stories.thread, *self.live.finalizer_threads)
                       if thread is not None and thread.is_alive()]
            self.drain_events()
            self.write_status("stopping" if workers else "stopped")
            if not workers:
                return True
            workers[0].join(timeout=0.5)
        logging.warning("Shutdown deadline reached; unfinished recording sessions remain recoverable on disk")
        self.write_status("shutdown_timeout")
        return False

    def run(self, once_seconds=None):
        self.enforce_disk_guard()
        deadline = time.monotonic() + once_seconds if once_seconds is not None else None
        try:
            self.live.start()
            self.stories.start()
            while not self.stop_event.is_set():
                self.enforce_disk_guard()
                self.drain_events()
                self.write_status("running")
                if not self.live.is_running() or (not self.stories_disk_stopped and not self.stories.is_running()):
                    raise RuntimeError("A recording monitor exited unexpectedly; service restart is required")
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.stop_event.wait(2)
        finally:
            self.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", "--validate", dest="check", action="store_true", help="Offline configuration validation (no probes)")
    parser.add_argument("--once", type=float, metavar="SECONDS", help="Run both engines for a bounded time, then stop gracefully")
    parser.add_argument("--status-file", help="Atomic status JSON path")
    args = parser.parse_args(argv)
    store = prepare_store(app.RecorderStore(read_only=True))
    summary = validate(store)
    if args.check:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    setup_logging()
    app.install_exception_hooks()
    with InstanceLock(app.APP_DIR / "douyin_headless.lock"):
        service = HeadlessService(store, args.status_file)
        for name in ("SIGTERM", "SIGINT"):
            signal.signal(getattr(signal, name), service.request_stop)
        service.run(args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
