"""Never-created capture parts must not abort finalize of the parts that exist."""
import json
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

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


def _recording(session, parts, output_file, *, part_index, last_size, profile_id="iis"):
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
        "Part 3 for iis produced no file (stream unavailable); dropping from part list."
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
    assert any(
        record.exc_info and record.exc_info[0] is FileNotFoundError
        for record in caplog.records
    )
    assert "No such file or directory" in recording["stop_reason"]
    assert str(vanished) in recording["stop_reason"]


def test_zero_byte_part_stays_in_list_and_is_skipped_in_concat(tmp_path, monkeypatch, caplog):
    session = tmp_path / "session"
    good = session / "part-0001.flv"
    empty = session / "part-0002.flv"
    session.mkdir()
    good.write_bytes(b"raw media")
    empty.write_bytes(b"")
    engine = _engine()
    recording = _recording(session, [good, empty], empty, part_index=2, last_size=0)

    with caplog.at_level(logging.WARNING):
        _exit(engine, recording, returncode=8)

    assert recording["parts"] == [str(good), str(empty)]
    assert empty.stat().st_size == 0
    assert "dropping from part list" not in caplog.text

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
    assert recording["parts"] == [str(good), str(empty)]
    listed = "\n".join(concat_lists)
    assert segments.concat_list_line(good) in listed
    assert segments.concat_list_line(empty) not in listed
    assert Path(recording["final_output"]).read_bytes() == b"remuxed media"
