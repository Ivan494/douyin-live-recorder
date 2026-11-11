"""Never-created capture parts must not abort finalize of the parts that exist."""
import errno
import json
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import douyin_recorder_app as app
import recording_segments as segments


def _engine():
    store = SimpleNamespace(
        settings={
            "ffmpeg_path": "fixture-ffmpeg",
            "ffprobe_path": "fixture-ffprobe",
            "recording_stall_timeout_seconds": 300,
            "recording_segment_max_seconds": 2700,
        },
        profiles=[],
    )
    return app.MonitorEngine(store, app.queue.Queue())


def _recording(session, parts, output_file, *, part_index, last_size, profile_id="test-profile"):
    session.mkdir(parents=True, exist_ok=True)
    return {
        "profile_id": profile_id,
        "session_dir": str(session),
        "manifest_path": str(session / "session.json"),
        "final_output": str(session.parent / "capture.mkv"),
        "output_file": str(output_file),
        "stderr_path": "",
        "started_at": time.time() - 120,
        "parts": [str(path) for path in parts],
        "part_index": part_index,
        "last_size": last_size,
        "segment_started_at": time.time() - 120,
    }


def _exit(engine, recording, returncode=8):
    profile_id = recording["profile_id"]
    process = SimpleNamespace(returncode=returncode, poll=lambda: returncode)
    engine.processes[profile_id] = process
    engine.recordings[profile_id] = recording
    engine._poll_processes()
    return recording


def test_never_created_part_is_dropped(tmp_path, caplog):
    session = tmp_path / "session"
    first = session / "part-0001.flv"
    missing = session / "part-0003.flv"
    later = session / "part-0005.flv"
    session.mkdir()
    first.write_bytes(b"first")
    later.write_bytes(b"later")
    engine = _engine()
    recording = _recording(session, [first, missing, later], missing, part_index=3, last_size=0)

    with caplog.at_level(logging.WARNING):
        _exit(engine, recording, returncode=8)

    assert not missing.exists()
    assert recording["parts"] == [str(first), str(later)]
    assert recording["part_index"] == 3
    assert recording["last_exit_code"] == 8
    assert (
        "Part 3 for test-profile produced no file (stream unavailable); dropping from part list."
        in caplog.text
    )
    saved = json.loads((session / "session.json").read_text(encoding="utf-8"))
    assert saved["parts"] == [str(first), str(later)]
    assert saved["part_index"] == 3


def test_existing_part_is_kept(tmp_path, caplog):
    session = tmp_path / "session"
    part = session / "part-0001.flv"
    session.mkdir()
    part.write_bytes(b"media")
    engine = _engine()
    recording = _recording(session, [part], part, part_index=1, last_size=part.stat().st_size)

    with caplog.at_level(logging.WARNING):
        _exit(engine, recording, returncode=0)

    assert recording["parts"] == [str(part)]
    assert recording["part_index"] == 1
    assert "dropping from part list" not in caplog.text
    saved = json.loads((session / "session.json").read_text(encoding="utf-8"))
    assert saved["parts"] == [str(part)]


def test_part_that_grew_then_vanished_is_kept_and_finalize_raises(tmp_path, caplog):
    session = tmp_path / "session"
    kept = session / "part-0001.flv"
    vanished = session / "part-0002.flv"
    session.mkdir()
    kept.write_bytes(b"kept-media")
    engine = _engine()
    recording = _recording(session, [kept, vanished], vanished, part_index=2, last_size=4096)

    with caplog.at_level(logging.WARNING):
        _exit(engine, recording, returncode=1)

    assert "dropping from part list" not in caplog.text
    assert recording["parts"] == [str(kept), str(vanished)]
    assert recording["last_size"] == 4096
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        engine._finalize_recording_worker(recording, "stream ended")

    assert recording["status"] == "failed"
    assert recording.get("publication_plan") is None
    assert not Path(recording["final_output"]).exists()
    assert kept.read_bytes() == b"kept-media"
    assert str(vanished) in recording["parts"]
    error = next(record.exc_info[1] for record in caplog.records
                 if record.exc_info and isinstance(record.exc_info[1], FileNotFoundError))
    assert error.errno == errno.ENOENT
    assert Path(error.filename) == vanished
    assert str(error) in recording["stop_reason"]



@pytest.mark.parametrize("file_created", [True, False])
def test_unwritten_part_does_not_block_finalization(tmp_path, monkeypatch, caplog, file_created):
    session = tmp_path / "session"
    good = session / "part-0001.flv"
    empty = session / "part-0002.flv"
    session.mkdir()
    good.write_bytes(b"raw media")
    if file_created:
        empty.write_bytes(b"")
    engine = _engine()
    recording = _recording(session, [good, empty], empty, part_index=2, last_size=0)

    with caplog.at_level(logging.WARNING):
        _exit(engine, recording, returncode=8)

    expected_parts = [str(good), str(empty)] if file_created else [str(good)]
    assert recording["parts"] == expected_parts
    assert recording["part_index"] == 2
    if file_created:
        assert empty.stat().st_size == 0
        assert "dropping from part list" not in caplog.text
    else:
        assert "dropping from part list" in caplog.text

    concat_lists = []

    def run(args, **_kwargs):
        if args[0] == "fixture-ffmpeg":
            concat = Path(args[args.index("-i") + 1])
            concat_lists.append(concat.read_text(encoding="utf-8"))
            Path(args[-1]).write_bytes(b"remuxed media")
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "streams": [{"codec_type": "video"}],
            "format": {"duration": "1"},
        }), "")

    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _value: "fixture-ffmpeg")
    monkeypatch.setattr(app, "resolve_trusted_executable", lambda *_args, **_kwargs: "fixture-ffprobe")
    monkeypatch.setattr(app.subprocess, "run", run)
    engine._finalize_recording_worker(recording, "stream ended")

    assert recording["status"] == "complete", recording.get("stop_reason")
    assert recording["parts"] == expected_parts
    listed = "\n".join(concat_lists)
    assert segments.concat_list_line(good) in listed
    assert segments.concat_list_line(empty) not in listed
    assert Path(recording["final_output"]).read_bytes() == b"remuxed media"


@pytest.mark.parametrize("change", ["missing", "truncated", "inaccessible"])
def test_observed_media_is_retained_after_a_live_poll(tmp_path, monkeypatch, change):
    session = tmp_path / "session"
    part = session / "part-0001.flv"
    session.mkdir()
    part.write_bytes(b"recorded media")
    engine = _engine()
    recording = _recording(session, [part], part, part_index=1, last_size=0)
    engine.recordings[recording["profile_id"]] = recording
    process = SimpleNamespace(returncode=None)
    process.poll = lambda: process.returncode
    engine.processes[recording["profile_id"]] = process
    engine._poll_processes()
    observed_size = part.stat().st_size
    assert recording["last_size"] == observed_size

    if change == "truncated":
        part.write_bytes(b"")
    elif change == "missing":
        part.unlink()
    with monkeypatch.context() as patch:
        if change == "inaccessible":
            original_stat = Path.stat
            def inaccessible(path, *args, **kwargs):
                if path == part:
                    raise PermissionError("temporary file access failure")
                return original_stat(path, *args, **kwargs)
            patch.setattr(Path, "stat", inaccessible)
        engine._poll_processes()
    assert recording["last_size"] == observed_size
    part.unlink(missing_ok=True)
    process.returncode = 1
    engine._poll_processes()
    assert recording["parts"] == [str(part)]
    engine._finalize_recording_worker(recording, "stream ended")
    assert recording["status"] == "failed"
    assert not Path(recording["final_output"]).exists()


@pytest.mark.parametrize("saved_size", [None, 0])
@pytest.mark.parametrize("empty_at_recovery", [False, True])
def test_recovered_unknown_part_is_not_assumed_never_created(tmp_path, saved_size, empty_at_recovery):
    session = tmp_path / "session"
    part = session / "part-0001.flv"
    session.mkdir()
    engine = _engine()
    recording = _recording(session, [part], part, part_index=1, last_size=0)
    # Older manifests contain no observation of the current part's size.
    if saved_size is None:
        recording.pop("last_size")
    if empty_at_recovery:
        part.touch()
    (session / "session.json").write_text(json.dumps(recording), encoding="utf-8")
    recovered = engine._recover_session_manifest(recording["profile_id"], str(part))
    engine.recordings[recording["profile_id"]] = recovered
    engine._recording_stalled(recording["profile_id"])
    part.unlink(missing_ok=True)
    _exit(engine, recovered)
    assert recovered["parts"] == [str(part)]


def test_inaccessible_current_part_is_not_dropped(tmp_path, monkeypatch):
    session = tmp_path / "session"
    part = session / "part-0001.flv"
    engine = _engine()
    recording = _recording(session, [part], part, part_index=1, last_size=0)
    original_stat = Path.stat
    def inaccessible(path, *args, **kwargs):
        if path == part:
            raise PermissionError("temporary file access failure")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", inaccessible)
    _exit(engine, recording)
    assert recording["parts"] == [str(part)]
    assert recording["status"] == "recovering"
