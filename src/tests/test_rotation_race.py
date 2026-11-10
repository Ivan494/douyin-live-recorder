"""A signed-URL refresh can lose the capture part that just exited.

The live monitor polls every process, then checks profiles. A rotation
SIGTERM can land, and FFmpeg can exit, while that same pass is still in an
earlier profile's stream check. ``_is_recording`` then reaps the exit via
``poll()`` and ``_start_recording`` replaces ``processes`` before the exit
path runs. ``rotation_stop_requested`` stays set, so every later rotation
is skipped.
"""
import itertools
import logging
import os
import queue
import signal
import subprocess
import time
from types import SimpleNamespace

# Windows Python does not define SIGKILL. The value is 9 on Linux.
SIGKILL = getattr(signal, "SIGKILL", 9)

import pytest

import douyin_recorder_app as app


_PIDS = itertools.count(2_100_000_001)
_STOP_FLAGS = (
    "rotation_stop_requested",
    "rotation_stop_requested_at",
    "rotation_term_resent",
    "rotation_kill_sent",
    "stall_stop_requested",
)


class FakeCapture:
    """FFmpeg stand-in. ``exit()`` is how the test makes ``poll()`` reap it."""

    def __init__(self, *args, **kwargs):
        self.pid = next(_PIDS)
        self.returncode = None
        self.signals = []
        self.stderr_file = kwargs.get("stderr")
        self.args = args

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")

    def exit(self, code=255):
        self.returncode = code


class AdoptedLike:
    """Windows-style adopted handle: ``terminate()`` exists, ``kill()`` does not."""

    def __init__(self, *args, **kwargs):
        self.pid = next(_PIDS)
        self.returncode = None
        self.signals = []
        self.stderr_file = kwargs.get("stderr")

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("TERM")

    def exit(self, code=-9):
        self.returncode = code


@pytest.fixture
def engine(tmp_path, monkeypatch):
    profile = {
        "id": "p1",
        "name": "rotation-profile",
        "url": "https://live.douyin.com/123",
        "output_dir": str(tmp_path),
        "enabled": True,
        "record_live": True,
    }
    store = SimpleNamespace(settings=app.default_settings(), profiles=[profile])
    monitor = app.MonitorEngine(store, queue.Queue())
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _path: "ffmpeg.exe")
    monkeypatch.setattr(app.subprocess, "Popen", FakeCapture)
    stream = SimpleNamespace(
        anchor_name="Anchor",
        title="Live",
        flv_url="https://example.test/live.flv",
        is_live=True,
    )
    yield monitor, profile, stream
    for handle in list(monitor.stderr_files.values()):
        if handle is not None and not handle.closed:
            handle.close()


def start(monitor, profile, stream):
    with monitor.lock:
        monitor._start_recording(profile, stream)
    return monitor.processes.get(profile["id"])


def expire(monitor, profile_id, *, age=2701):
    recording = monitor.recordings[profile_id]
    recording["rotate_at"] = time.time() - 1
    recording["segment_started_at"] = time.time() - age


def test_same_pass_exit_reaps_before_the_next_part_and_rearms_rotation(engine, monkeypatch, caplog):
    monitor, profile, stream = engine
    caplog.set_level(logging.INFO)
    spawned = []

    def popen(*args, **kwargs):
        spawned.append(monitor.processes.get(profile["id"]))
        return FakeCapture(*args, **kwargs)

    monkeypatch.setattr(app.subprocess, "Popen", popen)
    first = start(monitor, profile, stream)
    expire(monitor, profile["id"])
    monitor._poll_processes()
    assert first.signals == ["TERM"]
    # The stall flag is per part. A respawn that skips the exit path used to keep it.
    monitor.recordings[profile["id"]]["stall_stop_requested"] = True
    first.exit(255)
    # Same pass, later: the profile loop polls, sees "not recording", and starts part 2.
    assert not monitor._is_recording(profile["id"])
    second = start(monitor, profile, stream)

    assert spawned == [None, None]
    assert second is not first
    assert first.stderr_file.closed
    assert monitor.recordings[profile["id"]]["part_index"] == 2
    assert monitor.recording_error_counts.get(profile["id"], 0) == 0
    for flag in _STOP_FLAGS:
        assert flag not in monitor.recordings[profile["id"]]
    messages = [record.getMessage() for record in caplog.records]
    ended = next(index for index, message in enumerate(messages) if "Recording ended with code 255" in message)
    started = next(index for index, message in enumerate(messages) if "Starting capture part 2" in message)
    assert ended < started
    assert any("reaping exited FFmpeg" in message for message in messages)

    expire(monitor, profile["id"])
    monitor._poll_processes()
    assert second.signals == ["TERM"]


def test_start_recording_does_not_replace_a_live_ffmpeg(engine, caplog):
    monitor, profile, stream = engine
    caplog.set_level(logging.WARNING)
    first = start(monitor, profile, stream)
    second = start(monitor, profile, stream)
    assert second is first
    assert monitor.recordings[profile["id"]]["part_index"] == 1
    assert any("still running" in record.getMessage() for record in caplog.records)


def test_normal_rotation_still_reaps_on_the_next_pass(engine, caplog):
    monitor, profile, stream = engine
    caplog.set_level(logging.INFO)
    first = start(monitor, profile, stream)
    expire(monitor, profile["id"])
    monitor._poll_processes()
    assert first.signals == ["TERM"]
    first.exit(255)
    monitor._poll_processes()
    assert profile["id"] not in monitor.processes
    recording = monitor.recordings[profile["id"]]
    assert recording["status"] == "recovering"
    assert "rotation_stop_requested" not in recording
    assert any("Recording ended with code 255" in record.getMessage() for record in caplog.records)

    second = start(monitor, profile, stream)
    expire(monitor, profile["id"])
    monitor._poll_processes()
    assert second.signals == ["TERM"]


def test_rapid_death_during_respawn_keeps_the_backoff(engine):
    monitor, profile, stream = engine
    first = start(monitor, profile, stream)
    first.exit(1)
    second = start(monitor, profile, stream)
    assert second is None
    assert monitor.recording_error_counts[profile["id"]] == 1
    assert profile["id"] not in monitor.processes
    assert monitor.next_check[profile["id"]] > time.monotonic()
    # The backoff is the next check time. A later check may start the following part.
    third = start(monitor, profile, stream)
    assert third is not None
    assert monitor.recordings[profile["id"]]["part_index"] == 2


def test_finalize_on_exit_during_respawn_does_not_start_another_part(engine, monkeypatch):
    monitor, profile, stream = engine
    first = start(monitor, profile, stream)
    monitor.recordings[profile["id"]]["finalize_on_exit"] = True
    monitor.recordings[profile["id"]]["stop_reason"] = "Recorder stopped"
    first.exit(0)
    monkeypatch.setattr(monitor, "_finalize_recording_worker", lambda *_args, **_kwargs: None)
    second = start(monitor, profile, stream)
    for thread in monitor.finalizer_threads:
        thread.join(timeout=2)
    assert second is None
    assert profile["id"] not in monitor.processes
    assert profile["id"] not in monitor.recordings


def _record_force_kills(monkeypatch):
    runs = []
    os_kills = []

    def fake_run(cmd, **kwargs):
        runs.append((list(cmd), kwargs.get("timeout")))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    monkeypatch.setattr(app.os, "kill", lambda pid, sig: os_kills.append((pid, sig)))
    return runs, os_kills


def test_rotation_stop_resends_term_then_force_kills(engine, monkeypatch):
    monitor, profile, stream = engine
    runs, os_kills = _record_force_kills(monkeypatch)
    proc = start(monitor, profile, stream)
    expire(monitor, profile["id"])
    monitor._poll_processes()
    assert proc.signals == ["TERM"]

    monitor.recordings[profile["id"]]["rotation_stop_requested_at"] = time.time() - 50
    monitor._poll_processes()
    assert proc.signals == ["TERM"]
    assert runs == [] and os_kills == []

    monitor.recordings[profile["id"]]["rotation_stop_requested_at"] = time.time() - 61
    monitor._poll_processes()
    assert proc.signals == ["TERM", "TERM"]
    monitor._poll_processes()
    assert proc.signals == ["TERM", "TERM"]

    monitor.recordings[profile["id"]]["rotation_stop_requested_at"] = time.time() - 91
    monitor._poll_processes()
    if os.name == "nt":
        assert proc.signals == ["TERM", "TERM"]
        assert os_kills == []
        command, timeout = runs[-1]
        assert command[:4] == ["taskkill", "/F", "/PID", str(proc.pid)]
        assert command[-1] == "/T"
        assert timeout == 10
    else:
        assert proc.signals == ["TERM", "TERM", "KILL"]
        assert runs == [] and os_kills == []
    monitor._poll_processes()
    if os.name == "nt":
        assert len(runs) == 1
    else:
        assert proc.signals == ["TERM", "TERM", "KILL"]
    assert monitor.recordings[profile["id"]]["rotation_kill_sent"] is True

    proc.exit(-9)
    monitor._poll_processes()
    assert profile["id"] not in monitor.processes
    recording = monitor.recordings[profile["id"]]
    for flag in _STOP_FLAGS:
        assert flag not in recording
    assert recording["status"] == "recovering"


def test_rotation_force_kill_does_not_require_a_kill_method(engine, monkeypatch):
    monitor, profile, stream = engine
    runs, os_kills = _record_force_kills(monkeypatch)
    monkeypatch.setattr(app.subprocess, "Popen", AdoptedLike)
    proc = start(monitor, profile, stream)
    assert not hasattr(proc, "kill")
    expire(monitor, profile["id"])
    monitor._poll_processes()
    monitor.recordings[profile["id"]]["rotation_stop_requested_at"] = time.time() - 61
    monitor._poll_processes()
    assert proc.signals == ["TERM", "TERM"]
    assert runs == [] and os_kills == []
    monitor.recordings[profile["id"]]["rotation_stop_requested_at"] = time.time() - 91
    monitor._poll_processes()
    assert proc.signals == ["TERM", "TERM"]
    if os.name == "nt":
        command, timeout = runs[-1]
        assert command[:4] == ["taskkill", "/F", "/PID", str(proc.pid)]
        assert "/T" in command
        assert timeout == 10
        assert os_kills == []
    else:
        assert os_kills == [(proc.pid, SIGKILL)]
        assert runs == []


def test_force_kill_process_covers_windows_taskkill_and_posix_without_kill(monkeypatch):
    runs = []
    os_kills = []
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda cmd, **kwargs: runs.append((list(cmd), kwargs.get("timeout"))) or subprocess.CompletedProcess(cmd, 0),
    )
    monkeypatch.setattr(app.os, "kill", lambda pid, sig: os_kills.append((pid, sig)))
    monkeypatch.setattr(app, "hidden_subprocess_kwargs", lambda: {})
    adopted = SimpleNamespace(pid=424242)

    monkeypatch.setattr(app.os, "name", "nt")
    app.force_kill_process(adopted)
    command, timeout = runs[-1]
    assert command == ["taskkill", "/F", "/PID", "424242", "/T"]
    assert timeout == 10
    assert os_kills == []

    monkeypatch.setattr(app.os, "name", "posix")
    app.force_kill_process(adopted)
    assert os_kills == [(424242, SIGKILL)]

    popen_like = SimpleNamespace(pid=424243, kill=lambda: os_kills.append("method"))
    app.force_kill_process(popen_like)
    assert os_kills == [(424242, SIGKILL), "method"]
