"""Publication must retain complete captures on every entry point."""
import io
import asyncio
import json
import logging
from pathlib import Path
import subprocess

import pytest

import douyin_recorder_app as app
import douyin_live_watcher as watcher


@pytest.mark.parametrize("short_output", [True, False])
def test_watcher_validates_duration_before_deleting_raw_capture(tmp_path, monkeypatch, short_output):
    session = tmp_path / ".recording_sessions" / "live-watcher"
    session.mkdir(parents=True)
    capture = session / "part-0001.flv"
    capture.write_bytes(b"raw capture")
    output = tmp_path / "live.mkv"
    monkeypatch.setattr(app, "prepare_recording_parts", lambda *_a: ([capture], "manifest\n"))
    monkeypatch.setattr(app, "flv_playback_span_ms", lambda _path: 10_000)
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _value: "ffmpeg")
    monkeypatch.setattr(app, "resolve_trusted_executable", lambda *_a, **_kw: "ffprobe")

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            duration = 1 if short_output else 10
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "streams": [{"codec_type": "video"}], "format": {"duration": str(duration)}}), "")
        assert kwargs.get("timeout", 0) > 0, "Every finalizer subprocess needs a deadline"
        Path(command[-1]).write_bytes(b"nonempty output")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    if short_output:
        with pytest.raises(RuntimeError):
            watcher._remux_capture("ffmpeg", capture, output, logging.getLogger(__name__), io.StringIO())
        assert capture.read_bytes() == b"raw capture"
        assert not output.exists()
    else:
        watcher._remux_capture("ffmpeg", capture, output, logging.getLogger(__name__), io.StringIO())
        assert output.read_bytes() == b"nonempty output"
        assert not capture.exists()


def test_watcher_restart_resumes_saved_session_before_probing(tmp_path, monkeypatch):
    session = tmp_path / ".recording_sessions" / "live-watcher"
    session.mkdir(parents=True)
    capture = session / "part-0001.flv"
    capture.write_bytes(b"raw capture")
    (session / "session.json").write_text(json.dumps({
        "profile_id": "watcher", "final_output": str(tmp_path / "live.mkv"),
        "parts": [str(capture)], "publication_plan": {"state": "pending"},
    }))
    order = []
    monkeypatch.setattr(watcher, "STOP", False)
    monkeypatch.setattr(watcher, "resolve_trusted_executable", lambda *_a, **_kw: "ffmpeg")
    monkeypatch.setattr(watcher, "_watcher_capture_processes", lambda: [])
    monkeypatch.setattr(watcher, "_remux_capture", lambda *_a: order.append("resume"))

    async def probe(*_args):
        order.append("probe")
        watcher.STOP = True
        return {}, type("Stream", (), {"is_live": False})()

    monkeypatch.setattr(watcher, "resolve_stream", probe)
    asyncio.run(watcher.watch({"output_dir": str(tmp_path), "ffmpeg_path": "ffmpeg",
                               "poll_interval_seconds": 0}, logging.getLogger(__name__)))
    assert order == ["resume", "probe"]


@pytest.mark.parametrize("saved_pid", [True, False])
def test_restart_waits_for_orphan_writer_before_finalizing_or_probing(tmp_path, monkeypatch, saved_pid):
    session = tmp_path / ".recording_sessions" / "live-watcher"
    session.mkdir(parents=True)
    capture = session / "part-0001.flv"
    capture.write_bytes(b"initial packets")
    manifest = session / "session.json"
    manifest.write_text(json.dumps({
        "profile_id": "watcher", "final_output": str(tmp_path / "live.mkv"),
        "parts": [str(capture)], "status": "recording",
        "capture_pid": 42 if saved_pid else None, "capture_started_at": 123.0,
    }))
    monkeypatch.setattr(watcher, "STOP", False)
    alive = [True]
    monkeypatch.setattr(app, "pid_is_running", lambda pid, **_kw: bool(pid) and alive[0])
    monkeypatch.setattr(watcher, "resolve_trusted_executable", lambda *_a, **_kw: "ffmpeg")
    monkeypatch.setattr(watcher, "_watcher_capture_processes", lambda: [
        {"ProcessId": 42, "CommandLine": f'ffmpeg.exe -i https://example.test/live "{capture}"'}
    ] if alive[0] else [])
    order = []

    async def wait(_seconds):
        if watcher.STOP:
            return
        assert not order
        assert capture.read_bytes() == b"initial packets"
        capture.write_bytes(b"initial packets plus resumed packets")
        alive[0] = False

    def remux(*_args):
        assert not alive[0]
        assert capture.read_bytes().endswith(b"resumed packets")
        order.append("finalize")

    async def probe(*_args):
        order.append("probe")
        watcher.STOP = True
        return {}, type("Stream", (), {"is_live": False})()

    monkeypatch.setattr(watcher.asyncio, "sleep", wait)
    monkeypatch.setattr(watcher, "_remux_capture", remux)
    monkeypatch.setattr(watcher, "resolve_stream", probe)
    asyncio.run(watcher.watch({"output_dir": str(tmp_path), "ffmpeg_path": "ffmpeg",
                               "poll_interval_seconds": 1}, logging.getLogger(__name__)))
    assert order == ["finalize", "probe"]


def test_recovery_process_scan_failure_preserves_raw_without_publishing(tmp_path, monkeypatch):
    session = tmp_path / ".recording_sessions" / "live-watcher"
    session.mkdir(parents=True)
    raw = session / "part-0001.flv"
    raw.write_bytes(b"still recording")
    (session / "session.json").write_text("{}")

    def failed_scan():
        raise PermissionError("process scan unavailable")

    monkeypatch.setattr(watcher, "_watcher_capture_processes", failed_scan)
    assert not watcher.recover_watcher_recordings({"output_dir": str(tmp_path)}, logging.getLogger(__name__))
    assert raw.read_bytes() == b"still recording"
    assert not list(tmp_path.glob("*.mkv"))
