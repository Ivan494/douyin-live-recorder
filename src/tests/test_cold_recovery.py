"""Saved capture declarations must survive controller restart."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import douyin_recorder_app as app


@pytest.fixture
def saved(tmp_path):
    profile = {"id": "recovery-fixture", "name": "Recovery", "output_dir": str(tmp_path)}
    session = tmp_path / ".recording_sessions" / "capture"
    session.mkdir(parents=True)
    parts = [session / f"part-{i:04d}.flv" for i in range(1, 4)]
    for part in parts:
        part.write_bytes(b"recorded media")
    manifest_path = session / "session.json"
    data = {"profile_id": profile["id"], "parts": [str(p) for p in parts],
            "part_index": 3, "status": "recording", "final_output": str(tmp_path / "final.mkv"),
            "stderr_path": str(tmp_path / "capture.log")}
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    engine = app.MonitorEngine(SimpleNamespace(settings=app.default_settings(), profiles=[profile]), app.RecorderEventQueue())
    return SimpleNamespace(engine=engine, profile=profile, parts=parts, manifest=manifest_path, data=data)


@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_missing_declared_part_is_retained_and_prevents_short_publication(saved, missing_index):
    saved.parts[missing_index].unlink()
    saved.engine.recover_pending_recording_sessions()
    recording = saved.engine.recordings[saved.profile["id"]]
    assert recording["parts"] == saved.data["parts"]
    assert recording["output_file"] == str(saved.parts[-1])
    assert recording["part_index"] == 3
    assert json.loads(saved.manifest.read_text())["parts"] == saved.data["parts"]
    saved.engine._finalize_recording_worker(recording, "offline after recovery")
    assert recording["status"] == "failed"
    assert not Path(recording["final_output"]).exists()
    assert all(p.read_bytes() == b"recorded media" for i, p in enumerate(saved.parts) if i != missing_index)


@pytest.mark.parametrize("inaccessible_index", [0, 2])
def test_inspection_error_preserves_declarations(saved, monkeypatch, inaccessible_index):
    original = Path.stat
    def inspect(path, *args, **kwargs):
        if path == saved.parts[inaccessible_index]:
            raise PermissionError("fixture access failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", inspect)
    saved.engine.recover_pending_recording_sessions()
    recording = saved.engine.recordings[saved.profile["id"]]
    assert recording["parts"] == saved.data["parts"]
    assert json.loads(saved.manifest.read_text())["parts"] == saved.data["parts"]
    if inaccessible_index == 2:
        assert recording["last_size"] is None


def test_part_disappearing_during_inspection_is_retained(saved, monkeypatch):
    original = Path.stat
    vanished = False
    def inspect(path, *args, **kwargs):
        nonlocal vanished
        result = original(path, *args, **kwargs)
        if path == saved.parts[-1] and not vanished:
            vanished = True
            path.unlink()
        return result
    monkeypatch.setattr(Path, "stat", inspect)
    saved.engine.recover_pending_recording_sessions()
    assert vanished
    assert saved.engine.recordings[saved.profile["id"]]["parts"] == saved.data["parts"]


def test_all_missing_session_is_left_untouched(saved):
    for part in saved.parts:
        part.unlink()
    before = saved.manifest.read_bytes()
    saved.engine.recover_pending_recording_sessions()
    assert saved.profile["id"] not in saved.engine.recordings
    assert saved.manifest.read_bytes() == before


def test_empty_last_part_keeps_unknown_history(saved):
    saved.parts[-1].write_bytes(b"")
    saved.engine.recover_pending_recording_sessions()
    recording = saved.engine.recordings[saved.profile["id"]]
    assert recording["parts"] == saved.data["parts"]
    assert recording["last_size"] is None


def test_resume_keeps_missing_history_and_increments_number(saved, monkeypatch):
    saved.parts[1].unlink()
    saved.engine.recover_pending_recording_sessions()
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _path: "ffmpeg.exe")
    monkeypatch.setattr(app.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=2_100_000_123))
    stream = SimpleNamespace(anchor_name="Fixture", title="Recovery", flv_url="https://example.test/live.flv")
    try:
        saved.engine._start_recording(saved.profile, stream)
        recording = saved.engine.recordings[saved.profile["id"]]
        assert recording["part_index"] == 4
        assert recording["parts"][:-1] == saved.data["parts"]
        assert Path(recording["parts"][-1]).name == "part-0004.flv"
    finally:
        for handle in saved.engine.stderr_files.values():
            handle.close()
