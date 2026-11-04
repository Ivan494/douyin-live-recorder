"""Offline publication and raw-capture preservation fault injection."""
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import douyin_recorder_app as app


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Network access is forbidden in finalizer tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)



@pytest.fixture
def session(tmp_path, monkeypatch):
    store = SimpleNamespace(settings={"ffmpeg_path": "fixture-ffmpeg"}, profiles=[])
    engine = app.MonitorEngine(store, app.RecorderEventQueue())
    engine.emit = Mock()
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _setting: "fixture-ffmpeg")
    monkeypatch.setattr(app, "resolve_trusted_executable", lambda *_args, **_kwargs: "fixture-ffprobe")
    root = tmp_path / "acct" / ".recording_sessions" / "capture"
    root.mkdir(parents=True)
    parts = [root / f"part-{i}.mkv" for i in range(2)]
    for part in parts:
        part.write_bytes(b"raw media")
    final = tmp_path / "acct" / "capture.mkv"
    temporary = tmp_path / "acct" / "capture.finalizing.mkv"
    recording = {"profile_id": "fixture", "final_output": str(final), "session_dir": str(root),
                 "manifest_path": str(root / "session.json"), "parts": [str(p) for p in parts]}
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[0] == "fixture-ffmpeg":
            Path(args[-1]).write_bytes(b"remuxed media")
            return subprocess.CompletedProcess(args, 0)
        assert args[0] == "fixture-ffprobe", args
        duration = (sum(p.stat().st_size > 0 for p in parts)
                    if Path(args[-1]) == temporary else 1)
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": str(duration)},
        }), "")

    monkeypatch.setattr(app.subprocess, "run", run)
    return SimpleNamespace(engine=engine, root=root, parts=parts, final=final,
                           temporary=temporary, recording=recording, run=run, calls=calls)



def assert_preserved(session):
    assert session.recording["status"] == "failed"
    assert session.root.exists()
    assert not session.final.exists()
    assert json.loads((session.root / "session.json").read_text())["status"] == "failed"



def test_success_publishes_validated_output_then_cleans_raw_parts(session):
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert session.final.read_bytes() == b"remuxed media"
    assert session.recording["status"] == "complete" and not session.root.exists()
    command = session.calls[0]
    maps = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
    assert maps == ["0:v:0?", "0:a:0?"]
    assert "-xerror" in command
    assert len([args for args in session.calls if args[0] == "fixture-ffmpeg"]) == 1



def test_recorder_command_selects_first_video_and_audio(session, monkeypatch):
    process = Mock(pid=4242)
    popen = Mock(return_value=process)
    monkeypatch.setattr(app.subprocess, "Popen", popen)
    profile = {"id": "fixture", "name": "fixture", "output_dir": str(session.root.parent / "capture")}
    stream = SimpleNamespace(anchor_name="fixture", title="fixture", flv_url="https://example.test/live.flv")
    try:
        session.engine._start_recording(profile, stream)
        command = popen.call_args.args[0]
        maps = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
        assert maps == ["0:v:0?", "0:a:0?"]
        assert session.engine.recordings["fixture"]["parts"] == [command[-1]]
    finally:
        for handle in session.engine.stderr_files.values():
            handle.close()



@pytest.mark.parametrize("failure", ["ffmpeg-error", "timeout", "empty-output", "probe-error", "probe-json", "no-streams", "missing-probe"])
def test_failed_remux_or_probe_never_deletes_raw_parts(session, monkeypatch, failure):
    def run(args, **kwargs):
        if args[0] == "fixture-ffmpeg":
            if failure == "timeout":
                session.temporary.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(args, 30)
            if failure == "ffmpeg-error":
                session.temporary.write_bytes(b"partial")
                return subprocess.CompletedProcess(args, 1)
            result = session.run(args, **kwargs)
            if failure == "empty-output":
                Path(args[-1]).write_bytes(b"")
            return result
        if failure == "probe-error":
            return subprocess.CompletedProcess(args, 1, "", "invalid MKV")
        if failure == "probe-json":
            return subprocess.CompletedProcess(args, 0, "{torn", "")
        if failure == "no-streams":
            return subprocess.CompletedProcess(args, 0, '{"streams":[]}', "")
        return session.run(args, **kwargs)
    monkeypatch.setattr(app.subprocess, "run", run)
    if failure == "missing-probe":
        def unavailable(*_args, **_kwargs):
            raise ValueError("untrusted/missing probe")
        monkeypatch.setattr(app, "resolve_trusted_executable", unavailable)
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert_preserved(session)
    assert all(p.read_bytes() == b"raw media" for p in session.parts)



@pytest.mark.parametrize("problem", ["missing", "all-empty", "all-missing"])
def test_incomplete_part_list_never_silently_publishes_shorter_recording(session, problem):
    if problem == "all-empty":
        for part in session.parts:
            part.write_bytes(b"")
    else:
        session.parts[0].unlink()
        if problem == "all-missing":
            session.parts[1].unlink()
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert_preserved(session)
    assert not session.calls



def test_failed_connection_empty_part_can_finalize_completed_parts(session):
    session.parts[0].write_bytes(b"")
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert session.recording["status"] == "complete"
    assert session.final.exists() and not session.root.exists()



def test_empty_part_starting_to_grow_during_remux_prevents_cleanup(session, monkeypatch):
    session.parts[0].write_bytes(b"")
    def run(args, **kwargs):
        result = session.run(args, **kwargs)
        if args[0] == "fixture-ffmpeg":
            session.parts[0].write_bytes(b"new capture bytes")
        return result
    monkeypatch.setattr(app.subprocess, "run", run)
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert_preserved(session)
    assert session.parts[0].read_bytes() == b"new capture bytes"



@pytest.mark.parametrize("change", ["grow", "rewrite", "replace", "remove"])
def test_parts_changed_during_remux_are_preserved(session, monkeypatch, change):
    def run(args, **kwargs):
        result = session.run(args, **kwargs)
        if args[0] == "fixture-ffprobe":
            part = session.parts[0]
            if change == "grow":
                with part.open("ab") as handle:
                    handle.write(b"more media")
            elif change == "rewrite":
                part.write_bytes(b"new media")
            elif change == "replace":
                replacement = part.with_suffix(".new")
                replacement.write_bytes(part.read_bytes())
                replacement.replace(part)
            else:
                part.unlink()
        return result
    monkeypatch.setattr(app.subprocess, "run", run)
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert_preserved(session)
    assert session.parts[1].read_bytes() == b"raw media"



def test_publication_never_overwrites_previous_output(session):
    session.final.write_bytes(b"previous capture")
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert session.recording["status"] == "failed"
    assert session.final.read_bytes() == b"previous capture"
    assert all(p.exists() for p in session.parts)



def test_cleanup_failure_does_not_reclassify_published_recording(session, monkeypatch):
    def fail(_path):
        raise PermissionError("raw part held open")
    monkeypatch.setattr(app.shutil, "rmtree", fail)
    session.engine._finalize_recording_worker(session.recording, "fixture")
    assert session.recording["status"] == "complete"
    assert session.final.read_bytes() == b"remuxed media"
    assert all(p.exists() for p in session.parts)



def test_crash_during_concat_preserves_raw_and_can_retry(session, monkeypatch):
    def crash(*_args, **_kwargs):
        session.temporary.write_bytes(b"partial")
        raise KeyboardInterrupt("simulated process death")
    with monkeypatch.context() as patch:
        patch.setattr(app.subprocess, "run", crash)
        with pytest.raises(KeyboardInterrupt):
            session.engine._finalize_recording_worker(session.recording, "fixture")
    assert all(p.exists() for p in session.parts)
    assert not session.final.exists()
    assert json.loads((session.root / "session.json").read_text())["status"] == "finalizing"
    session.engine._finalize_recording_worker(session.recording, "retry")
    assert session.final.read_bytes() == b"remuxed media" and not session.root.exists()



def test_transient_strict_timeout_escalates_in_same_call(session, monkeypatch):
    attempts = []
    def run(args, **kwargs):
        if args[0] == 'fixture-ffmpeg':
            attempts.append(args)
            if len(attempts) == 1:
                raise subprocess.TimeoutExpired(args, 30)
        return session.run(args, **kwargs)
    monkeypatch.setattr(app.subprocess, 'run', run)
    session.engine._finalize_recording_worker(session.recording, 'transient timeout')
    assert session.recording['status'] == 'complete'
    assert len(attempts) == 2
    assert '-xerror' in attempts[0] and '-xerror' not in attempts[1]
    assert session.final.exists() and not session.root.exists()



def test_deadline_during_preparation_fails_once_with_raw_intact(session, monkeypatch):
    import recording_segments as segments
    now = [0.0]
    monkeypatch.setattr(segments.time, 'monotonic', lambda: now[0])
    def prepare(*_args):
        now[0] = 241.0
        segments.check_finalize_deadline()
    monkeypatch.setattr(app, 'prepare_recording_parts', prepare)
    session.engine._finalize_recording_worker(session.recording, 'deadline')
    assert_preserved(session)
    assert session.recording['finalize_attempts'] == 1
    assert not session.calls
    assert all(p.read_bytes() == b'raw media' for p in session.parts)
    log = (session.root / 'finalize.log').read_text()
    assert log.count('FINALIZE_FAILED') == 1 and 'deadline exhausted' in log


def test_interrupted_split_publication_resumes_without_remux(session, monkeypatch):
    staged = [session.root / f"piece-{i}.finalizing.mkv" for i in range(2)]
    for index, path in enumerate(staged):
        path.write_bytes(f"verified {index}".encode())
    monkeypatch.setattr(app, "finalize_prepared_recording", Mock(return_value=staged))
    original_publish = Path.rename if app.os.name == "nt" else app.os.link

    def interrupted(source, destination):
        if str(destination).endswith(".part02.mkv"):
            raise PermissionError("second output held open")
        return original_publish(source, destination)

    with monkeypatch.context() as patch:
        if app.os.name == "nt":
            patch.setattr(Path, "rename", interrupted)
        else:
            patch.setattr(app.os, "link", interrupted)
        session.engine._finalize_recording_worker(session.recording, "interrupted")
    first = session.final.with_name("capture.part01.mkv")
    assert first.read_bytes() == b"verified 0"
    assert session.recording["status"] == "failed"
    assert all(path.exists() for path in session.parts)
    saved = json.loads((session.root / "session.json").read_text())
    monkeypatch.setattr(app, "finalize_prepared_recording", Mock(side_effect=AssertionError("remux repeated")))
    session.engine._finalize_recording_worker(saved, "restart")
    assert saved["status"] == "complete"
    assert first.read_bytes() == b"verified 0"
    assert session.final.with_name("capture.part02.mkv").read_bytes() == b"verified 1"
    assert not session.root.exists()


@pytest.mark.skipif(app.os.name != "nt", reason="Windows removable-drive publication")
def test_publication_does_not_require_hard_links(session, monkeypatch):
    monkeypatch.setattr(app.os, "link", Mock(side_effect=OSError("unsupported filesystem")))
    session.engine._finalize_recording_worker(session.recording, "removable drive")
    assert session.recording["status"] == "complete"
    assert session.final.read_bytes() == b"remuxed media"
    assert not session.root.exists()


def test_media_flush_failure_preserves_raw_and_resumes_without_remux(session, monkeypatch):
    original_fsync = os.fsync

    def failed_media_flush(descriptor):
        if session.temporary.exists() and os.fstat(descriptor).st_ino == session.temporary.stat().st_ino:
            raise OSError("media flush failed")
        return original_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(app.os, "fsync", failed_media_flush)
        session.engine._finalize_recording_worker(session.recording, "flush failure")
    assert_preserved(session)
    assert all(path.read_bytes() == b"raw media" for path in session.parts)
    saved = json.loads((session.root / "session.json").read_text())
    monkeypatch.setattr(app, "finalize_prepared_recording", Mock(side_effect=AssertionError("remux repeated")))
    session.engine._finalize_recording_worker(saved, "restart")
    assert saved["status"] == "complete"
    assert session.final.read_bytes() == b"remuxed media"
    assert not session.root.exists()
