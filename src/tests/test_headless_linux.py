import importlib.abc
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import douyin_recorder_app as app
import douyin_headless as headless


def store_at(root):
    store = SimpleNamespace(settings={"headless_output_root": str(root), "ffmpeg_path": "ffmpeg",
                                     "headless_min_free_gb": 5, "headless_critical_free_gb": 1,
                                     "media_poll_interval_seconds": 60},
                           profiles=[{"id": "first", "name": "first", "url": "https://live.douyin.com/1234",
                                      "output_dir": str(root / "first"), "enabled": True,
                                      "auto_download_videos": True, "auto_download_stories": True},
                                     {"id": "disabled", "name": "disabled", "url": "https://live.douyin.com/5678",
                                      "output_dir": str(root / "disabled"), "enabled": False,
                                      "auto_download_videos": True, "auto_download_stories": True}])
    store.get_profile = lambda profile_id: next((p for p in store.profiles if p["id"] == profile_id), None)
    return store


def test_headless_import_without_gui_dependencies():
    source = """
import importlib.abc, sys
class RejectGUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'tkinter', 'pystray', 'PIL'}:
            raise RuntimeError('GUI dependency loaded: ' + fullname)
sys.meta_path.insert(0, RejectGUI())
import douyin_headless
assert douyin_headless.app.HEADLESS
"""
    result = subprocess.run([sys.executable, "-c", source], cwd=str(app.APP_DIR),
                            env={**os.environ, "DOUYIN_HEADLESS": "1"}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_imported_outputs_stay_under_root(tmp_path):
    root = tmp_path / "downloads"
    assert headless.normalize_output_path(r"E:\douyindownload\主播", "主播", root) == str(root / "主播")
    assert headless.normalize_output_path(r"first\stories", "first", root) == str(root / "first" / "stories")
    assert headless.normalize_output_path("../escaped", "first", root) == str(root / "first")
    assert headless.normalize_output_path(str(tmp_path / "elsewhere"), "first", root) == str(root / "first")
    assert headless.normalize_output_path(str(root / "valid"), "first", root) == str(root / "valid")


def test_portable_placeholder_backslashes_only_normalized_on_posix(monkeypatch, tmp_path):
    # Patch the name only while computing strings, without constructing Windows
    # pathlib objects on a POSIX runner (or vice versa).
    monkeypatch.setattr(app, "ROOT_DOWNLOAD_DIR", tmp_path)
    with monkeypatch.context() as patched:
        patched.setattr(app.os, "name", "posix")
        assert app.expand_portable_path(r"${DOWNLOAD_ROOT}\first\stories") == str(tmp_path).replace("\\", "/") + "/first/stories"
        assert app.expand_portable_path(r"unrelated\text") == r"unrelated\text"


def test_posts_never_download_and_disabled_profile_retained(monkeypatch, tmp_path):
    store = headless.prepare_store(store_at(tmp_path))
    assert len(store.profiles) == 2 and store.profiles[1]["enabled"] is False
    assert all(p["auto_download_videos"] is False for p in store.profiles)
    monkeypatch.setattr(app, "HEADLESS", True)
    download = Mock(return_value={"videos": {"status": "disabled"}, "stories": {"status": "no_active_stories"}})
    monkeypatch.setattr(app, "download_profile", download)
    disk = SimpleNamespace(can_start=lambda _profile: True)
    engine = headless.HeadlessStoryEngine(store, app.RecorderEventQueue(), disk)
    monkeypatch.setattr(engine, "_save_circuit_breaker_state", lambda: None)
    engine._check_profile(store.profiles[0])
    assert download.call_args.kwargs["videos"] is False
    assert download.call_args.kwargs["stories"] is True
    assert not engine.enabled_for_profile({"auto_download_videos": True, "auto_download_stories": False})


def test_disk_guard_blocks_new_capture_but_does_not_delete(tmp_path, monkeypatch):
    store = headless.prepare_store(store_at(tmp_path))
    usage = lambda _path: SimpleNamespace(free=4 * headless.GIB)
    disk = headless.DiskGuard(store, usage)
    assert disk.snapshot()["new_captures_paused"]
    assert not disk.snapshot()["critically_low"]
    engine = headless.HeadlessMonitorEngine(store, app.RecorderEventQueue(), disk)
    start = Mock()
    monkeypatch.setattr(app.MonitorEngine, "_start_recording", start)
    engine._start_recording(store.profiles[0], object())
    start.assert_not_called()


def test_low_disk_finalization_preserves_and_resumes_raw_parts(tmp_path, monkeypatch):
    store = headless.prepare_store(store_at(tmp_path))
    session = tmp_path / "first" / ".recording_sessions" / "session"
    session.mkdir(parents=True)
    part = session / "part-0001.mkv"
    part.write_bytes(b"video data")
    free = [0]
    disk = headless.DiskGuard(store, lambda _path: SimpleNamespace(free=free[0]))
    engine = headless.HeadlessMonitorEngine(store, app.RecorderEventQueue(), disk)
    manifest = {"profile_id": "first", "session_dir": str(session), "manifest_path": str(session / "session.json"),
                "parts": [str(part)], "final_output": str(tmp_path / "first" / "final.mkv")}
    engine._finalize_recording_worker(manifest, "low disk")
    assert part.read_bytes() == b"video data"
    assert json.loads((session / "session.json").read_text())["status"] == "waiting_for_disk"
    assert len(engine.deferred_finalizers) == 1
    finalize = Mock()
    monkeypatch.setattr(app.MonitorEngine, "_finalize_recording_worker", finalize)
    free[0] = 10 * headless.GIB
    engine.retry_deferred_finalizers()
    for thread in engine.finalizer_threads:
        thread.join(2)
    finalize.assert_called_once()
    assert not engine.deferred_finalizers


def test_disk_critical_stops_writers_and_resume_restarts_stories(tmp_path):
    service = headless.HeadlessService(store_at(tmp_path), tmp_path / "status.json")
    service.live.processes = {"first": object()}
    service.live.stop_profile_recording = Mock()
    service.stories.stop = Mock()
    running = [True]
    service.stories.is_running = lambda: running[0]
    service.stories.start = Mock()
    state = {"free_bytes": 4 * headless.GIB, "new_captures_paused": True, "critically_low": False}
    service.disk.snapshot = lambda: state
    service.enforce_disk_guard()
    service.live.stop_profile_recording.assert_not_called()
    service.stories.stop.assert_not_called()
    state = {"free_bytes": headless.GIB // 2, "new_captures_paused": True, "critically_low": True}
    service.enforce_disk_guard()
    service.live.stop_profile_recording.assert_called_once_with("first", "Critical low disk space")
    service.stories.stop.assert_called_once()
    running[0] = False
    state = {"free_bytes": 10 * headless.GIB, "new_captures_paused": False, "critically_low": False}
    service.enforce_disk_guard()
    service.stories.start.assert_called_once()
    assert not service.stories_disk_stopped


@pytest.mark.parametrize("status", ["waiting_for_disk", "finalizing"])
def test_restart_recovers_disk_deferred_session_without_resuming_broadcast(tmp_path, monkeypatch, status):
    store = headless.prepare_store(store_at(tmp_path))
    root = Path(store.profiles[0]["output_dir"]) / ".recording_sessions" / "old"
    root.mkdir(parents=True)
    part = root / "part-0001.mkv"
    part.write_bytes(b"old broadcast")
    (root / "session.json").write_text(json.dumps({"status": status, "profile_id": "first",
                                                   "parts": [str(part)], "final_output": str(tmp_path / "old.mkv")}))
    disk = headless.DiskGuard(store, lambda _path: SimpleNamespace(free=0))
    engine = headless.HeadlessMonitorEngine(store, app.RecorderEventQueue(), disk)
    monkeypatch.setattr(app, "HEADLESS", True)
    engine.recover_pending_recording_sessions()
    assert not engine.recordings
    assert len(engine.deferred_finalizers) == 1
    assert part.read_bytes() == b"old broadcast"


def test_concurrent_finalizers_serialize_and_recheck_available_space(tmp_path, monkeypatch):
    store = headless.prepare_store(store_at(tmp_path))
    free = [10 * headless.GIB]
    disk = headless.DiskGuard(store, lambda _path: SimpleNamespace(free=free[0]))
    engine = headless.HeadlessMonitorEngine(store, app.RecorderEventQueue(), disk)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    calls = []

    def finalize(_engine, recording, _reason):
        calls.append(recording["profile_id"])
        if recording["profile_id"] == "first":
            first_entered.set()
            assert release.wait(3)
        else:
            second_entered.set()

    monkeypatch.setattr(app.MonitorEngine, "_finalize_recording_worker", finalize)
    manifests = []
    for name in ("first", "second"):
        root = tmp_path / name / ".recording_sessions" / "test"
        root.mkdir(parents=True)
        part = root / "part-0001.mkv"
        part.write_bytes(b"raw")
        manifests.append({"profile_id": name, "session_dir": str(root), "manifest_path": str(root / "session.json"),
                          "parts": [str(part)], "final_output": str(tmp_path / name / "final.mkv")})
    one = threading.Thread(target=engine._finalize_recording_worker, args=(manifests[0], "test"))
    two = threading.Thread(target=engine._finalize_recording_worker, args=(manifests[1], "test"))
    one.start()
    assert first_entered.wait(3)
    two.start()
    try:
        assert not second_entered.wait(0.1)
        # Space disappears while the second finalizer is waiting. It must use
        # the current free space after acquiring the lock, not the earlier value.
        free[0] = 0
    finally:
        release.set()
        one.join(3)
        two.join(3)
    assert not one.is_alive() and not two.is_alive()
    assert calls == ["first"]
    assert len(engine.deferred_finalizers) == 1
    assert json.loads(Path(manifests[1]["manifest_path"]).read_text())["status"] == "waiting_for_disk"


def test_posix_kill_uses_process_api(monkeypatch):
    process = Mock(pid=123)
    run = Mock(side_effect=AssertionError("Windows subprocess should not run"))
    monkeypatch.setattr(app.subprocess, "run", run)
    with monkeypatch.context() as patched:
        patched.setattr(app.os, "name", "posix")
        app.force_kill_process(process)
    process.kill.assert_called_once()
    run.assert_not_called()


def test_status_and_console_redact_secrets(tmp_path):
    status = tmp_path / "status.json"
    headless.atomic_status(status, {"cookies": "sessionid=secret", "url": "https://cdn.example/video?token=secret",
                                    "message": "Cookie: sessionid=secret; uid_tt=secret"})
    payload = status.read_text()
    assert "secret" not in payload
    assert "[redacted]" in payload
    assert not list(tmp_path.glob("*.tmp.*"))


def test_shutdown_cancels_workers_and_waits_for_finalizer(tmp_path):
    service = headless.HeadlessService(store_at(tmp_path), tmp_path / "status.json")
    stopped = threading.Event()
    finalizer_done = threading.Event()
    worker = threading.Thread(target=lambda: stopped.wait(3), daemon=True)
    finalizer = threading.Thread(target=lambda: (time.sleep(0.05), finalizer_done.set()))
    worker.start()
    finalizer.start()
    service.live.thread = worker
    service.live.finalizer_threads = [finalizer]
    service.live.stop = lambda terminate_recordings=True: stopped.set()
    service.stories.stop = lambda: None
    assert service.shutdown()
    assert finalizer_done.is_set()
    assert json.loads(service.status_path.read_text())["phase"] == "stopped"


def test_offline_validation_makes_no_network_calls(monkeypatch, tmp_path):
    store = headless.prepare_store(store_at(tmp_path))
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _path: "ffmpeg")
    monkeypatch.setattr(app, "resolve_trusted_executable", lambda *_args, **_kwargs: "ffprobe")
    monkeypatch.setattr(app.MonitorEngine, "_resolve", Mock(side_effect=AssertionError("network probe")))
    summary = headless.validate(store)
    assert summary["profiles"] == 2
    assert summary["enabled_profiles"] == 1
    assert summary["post_downloads"] is False


@pytest.mark.skipif(os.name == "nt", reason="Linux flock test")
def test_singleton_lock_excludes_second_process(tmp_path):
    lock = tmp_path / "recorder.lock"
    with headless.InstanceLock(lock):
        source = "from douyin_headless import InstanceLock\nwith InstanceLock(__import__('sys').argv[1]): pass"
        result = subprocess.run([sys.executable, "-c", source, str(lock)], cwd=str(app.APP_DIR),
                                env={**os.environ, "DOUYIN_HEADLESS": "1"}, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "already running" in result.stderr
    with headless.InstanceLock(lock):
        pass
