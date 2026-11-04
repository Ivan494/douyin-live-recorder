"""Fault injection around finalizer publication and raw-part cleanup, offline."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DOUYIN_HEADLESS", "1")
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


def test_publication_failure_keeps_previous_output_and_raw_parts(session, monkeypatch):
    session.final.write_bytes(b"previous capture")
    replace = Path.replace
    def fail(self, target):
        if self == session.temporary:
            raise PermissionError("destination held open")
        return replace(self, target)
    monkeypatch.setattr(Path, "replace", fail)
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


@pytest.mark.parametrize('crash', [False, True])
@pytest.mark.parametrize('corruption', ['none', 'manifest', 'manifest-and-gate'])
def test_interrupted_batch_is_gated_and_restart_finishes_same_plan(session, monkeypatch, crash, corruption):
    import importlib.util
    spec = importlib.util.spec_from_file_location('batch_uploader',
        Path(__file__).resolve().parents[1] / 'uploader' / 'drive_uploader.py')
    uploader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(uploader)
    stages = []
    def split(_ffmpeg, _inputs, _manifest, _temp, remux, *_args, **_kwargs):
        # A retry must resume the batch, even if this strategy would now return
        # a single different output. Prohibit a second ladder invocation.
        assert not stages
        remux.mkdir()
        outputs = [remux / f'part{i}.finalizing.mkv' for i in (1, 2)]
        for i, output in enumerate(outputs):
            output.write_bytes(f'verified media {i}'.encode())
        stages.extend(outputs)
        return outputs
    monkeypatch.setattr(app, 'finalize_prepared_recording', split)
    replace = Path.replace
    def interrupt(source, destination):
        if Path(destination).name == 'capture.part02.mkv':
            if crash:
                raise KeyboardInterrupt('process exit after first rename')
            raise PermissionError('second destination locked')
        if Path(destination).name == 'capture.part01.mkv':
            saved = json.loads((session.root / 'session.json').read_text())
            assert saved['publication_plan']['state'] == 'pending'
            assert len(saved['publication_plan']['members']) == 2
            gate = json.loads((session.final.parent / '.capture.publication.json').read_text())
            assert gate['destinations'] == [member['destination'] for member in saved['publication_plan']['members']]
        return replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', interrupt)
        if crash:
            with pytest.raises(KeyboardInterrupt):
                session.engine._finalize_recording_worker(session.recording, 'batch')
        else:
            session.engine._finalize_recording_worker(session.recording, 'batch')
    first = session.final.with_name('capture.part01.mkv')
    second = session.final.with_name('capture.part02.mkv')
    assert first.exists() and not second.exists()
    assert stages[1].exists() and all(p.exists() for p in session.parts)
    saved = json.loads((session.root / 'session.json').read_text())
    assert saved['publication_plan']['state'] == 'pending'
    if corruption != 'none':
        (session.root / 'session.json').write_text('{torn')
    if corruption == 'manifest-and-gate':
        (session.final.parent / '.capture.publication.json').write_text('{torn')
    unrelated = session.final.parent / 'normal.mkv'
    unrelated.write_bytes(b'ordinary finished recording')
    # Actual uploader, with unrelated eligibility guards removed. The batch
    # prefix remains ineligible after the failed worker has stopped.
    monkeypatch.setattr(uploader, 'ROOT', session.root.parent.parent.parent)
    monkeypatch.setattr(uploader, 'MIN_AGE', -1)
    monkeypatch.setattr(uploader, 'open_files', lambda: set())
    monkeypatch.setattr(uploader, 'busy_profiles', lambda: set())
    assert {p for p, _, _ in uploader.candidates()} == {unrelated}
    assert not uploader.upload_one(first, first.relative_to(uploader.ROOT), first.stat().st_size, False)
    # Repair deliberately corrupted recovery state before resuming its plan.
    if corruption != 'none':
        session.engine._save_recording_manifest(saved)
    session.engine._finalize_recording_worker(saved, 'restart')
    assert saved['status'] == 'complete' and saved['publication_plan']['state'] == 'committed'
    assert saved['final_outputs'] == [str(first), str(second)]
    assert first.read_bytes() == b'verified media 0' and second.read_bytes() == b'verified media 1'
    assert not session.final.exists() and not session.root.exists()
    assert {p for p, _, _ in uploader.candidates()} == {first, second, unrelated}
    assert not (session.final.parent / '.capture.publication.json').exists()


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


@pytest.fixture
def pending_batch(session, monkeypatch):
    """Actual caught publication failure, with the first member renamed."""
    profile = {"id": "fixture", "name": "fixture", "url": "https://live.douyin.com/fixture",
               "output_dir": str(session.final.parent)}
    session.engine.store.profiles = [profile]
    session.engine.store.settings.update(app.default_settings())

    def split(_ffmpeg, _inputs, _manifest, _temp, remux, *_args, **_kwargs):
        remux.mkdir()
        outputs = [remux / f"part{i}.finalizing.mkv" for i in (1, 2)]
        for output in outputs:
            output.write_bytes(b"verified media")
        return outputs

    monkeypatch.setattr(app, "finalize_prepared_recording", split)
    outputs = [session.final.with_name(f"capture.part{i:02d}.mkv") for i in (1, 2)]
    replace = Path.replace

    def interrupt(source, destination):
        if Path(destination) == outputs[1]:
            raise PermissionError("second destination locked")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", interrupt)
        session.engine._finalize_recording_worker(session.recording, "publication interrupted")
    saved = json.loads((session.root / "session.json").read_text())
    assert saved["status"] == "failed" and saved["publication_plan"]["state"] == "pending"
    assert outputs[0].exists() and not outputs[1].exists()
    monkeypatch.setattr(app, "finalize_prepared_recording", Mock(side_effect=AssertionError("ladder forbidden")))
    session.profile, session.saved, session.outputs = profile, saved, outputs
    return session


def assert_batch_published(batch, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("recovery_uploader",
        Path(__file__).resolve().parents[1] / "uploader" / "drive_uploader.py")
    uploader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(uploader)
    monkeypatch.setattr(uploader, "ROOT", batch.final.parent.parent)
    monkeypatch.setattr(uploader, "MIN_AGE", -1)
    monkeypatch.setattr(uploader, "open_files", lambda: set())
    monkeypatch.setattr(uploader, "busy_profiles", lambda: set())
    assert all(p.read_bytes() == b"verified media" for p in batch.outputs)
    assert not batch.root.exists() and not any(p.exists() for p in batch.parts)
    assert not batch.final.exists()
    assert {p for p, _, _ in uploader.candidates()} == set(batch.outputs)
    app.finalize_prepared_recording.assert_not_called()


@pytest.mark.parametrize("entry", ["startup", "deferred", "worker"])
def test_headless_saved_batch_needs_only_critical_reserve(pending_batch, monkeypatch, entry):
    import douyin_headless as headless
    batch = pending_batch
    # Free space is below a full media reservation, but above critical.
    free = [999 if entry == "deferred" else 1001]
    disk = SimpleNamespace(critical=1000, free=lambda _path: free[0])
    engine = headless.HeadlessMonitorEngine(batch.engine.store, app.RecorderEventQueue(), disk)
    monkeypatch.setattr(app, "HEADLESS", True)
    assert engine._finalize_space_needed(dict(batch.saved, publication_plan=None)) > 1001
    assert engine._finalize_space_needed(batch.saved) == 1000
    completed = []
    resume = engine._resume_recording_publication

    def tracked(recording):
        assert engine.finalize_lock.locked()
        result = resume(recording)
        persisted = json.loads((batch.root / "session.json").read_text())
        assert persisted["publication_plan"]["state"] == "committed"
        assert persisted["status"] == "complete"
        completed.append(recording)
        return result

    monkeypatch.setattr(engine, "_resume_recording_publication", tracked)
    if entry == "worker":
        engine._finalize_recording_worker(batch.saved, "resume")
    else:
        engine.recover_pending_recording_sessions()
        assert not engine.recordings
        assert len(engine.deferred_finalizers) == 1
        engine.retry_deferred_finalizers()
        if entry == "deferred":
            assert not engine.finalizer_threads and len(engine.deferred_finalizers) == 1
            free[0] = 1001
            engine.retry_deferred_finalizers()
        for thread in engine.finalizer_threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert not engine.deferred_finalizers
    assert len(completed) == 1
    assert_batch_published(batch, monkeypatch)


@pytest.mark.parametrize("status", ["finalizing", "failed"])
def test_desktop_startup_publishes_saved_batch_and_next_live_uses_fresh_session(pending_batch, monkeypatch, status):
    batch = pending_batch
    batch.saved["status"] = status
    batch.engine._save_recording_manifest(batch.saved)
    engine = app.MonitorEngine(batch.engine.store, app.RecorderEventQueue())
    monkeypatch.setattr(app, "HEADLESS", False)
    entered, release = threading.Event(), threading.Event()
    resume = engine._resume_recording_publication
    completed = []

    def blocked_resume(recording):
        entered.set()
        assert release.wait(timeout=5)
        result = resume(recording)
        completed.append(recording)
        return result

    monkeypatch.setattr(engine, "_resume_recording_publication", blocked_resume)
    stream = SimpleNamespace(is_live=True, flv_url="https://example.test/live.flv",
                             anchor_name="fixture", title="fixture")

    async def resolve(_profile):
        return {}, stream

    monkeypatch.setattr(engine, "_resolve", resolve)

    def capture(command, **_kwargs):
        Path(command[-1]).write_bytes(b"new broadcast")
        return Mock(pid=42)

    monkeypatch.setattr(app.subprocess, "Popen", capture)
    try:
        engine.recover_pending_recording_sessions()
        assert entered.wait(timeout=5)
        assert not engine.recordings
        # Repeated startup must not queue a concurrent copy of this batch.
        engine.recover_pending_recording_sessions()
        assert len(engine.finalizer_threads) == 1
        engine._check_profile(batch.profile)
        fresh = engine.recordings["fixture"]
        assert fresh["session_dir"] != str(batch.root)
        assert "publication_plan" not in fresh
        assert len(fresh["parts"]) == 1
        assert Path(fresh["parts"][0]).read_bytes() == b"new broadcast"
        assert all(p.read_bytes() == b"raw media" for p in batch.parts)
        assert json.loads((batch.root / "session.json").read_text())["parts"] == batch.saved["parts"]
    finally:
        release.set()
        for thread in engine.finalizer_threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        for handle in engine.stderr_files.values():
            handle.close()
    assert len(completed) == 1
    assert completed[0]["status"] == "complete"
    assert completed[0]["publication_plan"]["state"] == "committed"
    assert_batch_published(batch, monkeypatch)


def test_desktop_without_plan_recovers_capture_in_same_session(session, monkeypatch):
    profile = {"id": "fixture", "name": "fixture", "url": "https://live.douyin.com/fixture",
               "output_dir": str(session.final.parent)}
    session.engine.store.profiles = [profile]
    session.engine.store.settings.update(app.default_settings())
    session.recording.update(status="recording", part_index=2, stderr_path=str(session.final.parent / "capture.log"))
    session.engine._save_recording_manifest(session.recording)
    monkeypatch.setattr(app, "HEADLESS", False)
    stream = SimpleNamespace(is_live=True, flv_url="https://example.test/live.flv",
                             anchor_name="fixture", title="fixture")

    async def resolve(_profile):
        return {}, stream

    monkeypatch.setattr(session.engine, "_resolve", resolve)
    monkeypatch.setattr(app.subprocess, "Popen", Mock(return_value=Mock(pid=42)))
    try:
        session.engine.recover_pending_recording_sessions()
        recovered = session.engine.recordings["fixture"]
        assert recovered["status"] == "recovering" and not session.engine.finalizer_threads
        session.engine._check_profile(profile)
        assert recovered["session_dir"] == str(session.root)
        assert recovered["parts"][:2] == session.recording["parts"]
        assert recovered["parts"][2] == str(session.root / "part-0003.flv")
        assert recovered["status"] == "recording" and not recovered.get("publication_plan")
    finally:
        for handle in session.engine.stderr_files.values():
            handle.close()


@pytest.mark.parametrize('entry', ['worker', 'startup', 'deferred'])
def test_headless_normal_flv_finalizes_at_baseline_disk_threshold(session, monkeypatch, entry):
    import douyin_headless as headless
    import recording_segments as segments
    parts = [path.with_suffix('.flv') for path in session.parts]
    for old, new in zip(session.parts, parts):
        old.replace(new)
    session.parts[:] = parts
    session.recording.update(parts=[str(p) for p in parts], status='finalizing')
    profile = {'id': 'fixture', 'output_dir': str(session.final.parent)}
    session.engine.store.profiles = [profile]
    size = sum(path.stat().st_size for path in parts)
    free = [2 * size + 999 if entry == 'deferred' else 2 * size + 1000]
    disk = SimpleNamespace(critical=1000, free=lambda _path: free[0])
    engine = headless.HeadlessMonitorEngine(session.engine.store, app.RecorderEventQueue(), disk)
    monkeypatch.setattr(app, 'HEADLESS', True)
    monkeypatch.setattr(app, 'flv_playback_span_ms', lambda _path: 1000)
    monkeypatch.setattr(app, 'prepare_recording_parts', lambda *_args: (
        parts, 'ffconcat version 1.0\n' + '\n'.join(segments.concat_list_line(p) for p in parts)))
    monkeypatch.setattr(engine, '_check_finalize_normalization_space', Mock(side_effect=AssertionError('normalization forbidden')))
    engine._save_recording_manifest(session.recording)
    assert engine._finalize_space_needed(session.recording) == 2 * size + 1000
    if entry == 'worker':
        engine._finalize_recording_worker(session.recording, 'ordinary stop')
        assert session.recording['status'] == 'complete'
    else:
        engine.recover_pending_recording_sessions()
        engine.retry_deferred_finalizers()
        if entry == 'deferred':
            assert not engine.finalizer_threads and len(engine.deferred_finalizers) == 1
            free[0] += 1
            engine.retry_deferred_finalizers()
        for thread in engine.finalizer_threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
    remuxes = [args for args in session.calls if args[0] == 'fixture-ffmpeg']
    probes = [args for args in session.calls if args[0] == 'fixture-ffprobe']
    assert len(remuxes) == len(probes) == 1 and '-xerror' in remuxes[0]
    assert session.final.read_bytes() == b'remuxed media'
    assert not session.root.exists() and not any(p.exists() for p in parts)
    assert not engine.deferred_finalizers
    engine._check_finalize_normalization_space.assert_not_called()


@pytest.mark.parametrize('enough_space', [False, True])
def test_headless_checks_extra_copies_only_at_normalization_under_lock(session, monkeypatch, enough_space):
    import douyin_headless as headless
    size = sum(p.stat().st_size for p in session.parts)
    normalization_needed = int(2 * size * 1.2) + 1000
    free = [normalization_needed if enough_space else normalization_needed - 1]
    disk = SimpleNamespace(critical=1000, free=lambda _path: free[0])
    engine = headless.HeadlessMonitorEngine(session.engine.store, app.RecorderEventQueue(), disk)
    assert free[0] >= engine._finalize_space_needed(session.recording)
    checked = []
    check = engine._check_finalize_normalization_space
    def tracked(recording, inputs):
        assert engine.finalize_lock.locked()
        assert len([c for c in session.calls if c[0] == 'fixture-ffmpeg']) == 2
        assert not session.temporary.exists()
        assert not (session.root / 'finalize_remux').exists()
        checked.append(True)
        return check(recording, inputs)
    monkeypatch.setattr(engine, '_check_finalize_normalization_space', tracked)
    def run(command, **kwargs):
        if command[0] == 'fixture-ffmpeg' and Path(command[command.index('-i') + 1]).name == 'parts.txt':
            session.calls.append(command)
            session.temporary.write_bytes(b'failed concat')
            return subprocess.CompletedProcess(command, 234)
        return session.run(command, **kwargs)
    monkeypatch.setattr(app.subprocess, 'run', run)
    engine._finalize_recording_worker(session.recording, 'extra space')
    assert checked == [True]
    if enough_space:
        assert session.recording['status'] == 'complete'
        assert session.final.exists() and not session.root.exists()
        assert len([c for c in session.calls if c[0] == 'fixture-ffmpeg']) == 5
    else:
        assert_preserved(session)
        assert session.recording['finalize_attempts'] == 1
        assert 'Insufficient disk space for normalization' in session.recording['stop_reason']
        assert all(p.read_bytes() == b'raw media' for p in session.parts)
        assert len(session.calls) == 2
        assert not session.temporary.exists() and not (session.root / 'finalize_remux').exists()
        # A later retry may escalate after space recovers.
        free[0] += 1
        monkeypatch.setattr(engine, '_check_finalize_normalization_space', check)
        engine._finalize_recording_worker(session.recording, 'disk recovered')
        assert session.recording['status'] == 'complete' and not session.root.exists()


@pytest.mark.parametrize('boundary', ['gate-rename', 'gate-fsync', 'commit', 'gate-removal'])
def test_publication_gate_survives_interruption_at_durability_boundaries(session, monkeypatch, boundary):
    import importlib.util
    spec = importlib.util.spec_from_file_location('durability_uploader',
        Path(__file__).resolve().parents[1] / 'uploader' / 'drive_uploader.py')
    uploader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(uploader)
    gate = session.final.parent / '.capture.publication.json'
    replace, unlink = Path.replace, Path.unlink
    save = session.engine._save_recording_manifest
    sync = session.engine._sync_recording_directory
    sync_calls = []
    def interrupt_replace(source, destination):
        if boundary == 'gate-rename' and Path(destination) == gate:
            raise OSError('gate rename failed')
        return replace(source, destination)
    def interrupt_sync(path):
        sync_calls.append(path)
        if boundary == 'gate-fsync' and len(sync_calls) == 1:
            raise OSError('gate directory fsync failed')
        return sync(path)
    def interrupt_save(recording):
        if boundary == 'commit' and recording.get('status') == 'complete':
            raise OSError('manifest commit failed')
        return save(recording)
    def interrupt_unlink(path, *args, **kwargs):
        if boundary == 'gate-removal' and path == gate:
            raise KeyboardInterrupt('exit after durable commit')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', interrupt_replace)
        patch.setattr(Path, 'unlink', interrupt_unlink)
        patch.setattr(session.engine, '_save_recording_manifest', interrupt_save)
        patch.setattr(session.engine, '_sync_recording_directory', interrupt_sync)
        if boundary == 'gate-removal':
            with pytest.raises(KeyboardInterrupt):
                session.engine._finalize_recording_worker(session.recording, boundary)
        else:
            session.engine._finalize_recording_worker(session.recording, boundary)
    assert all(p.read_bytes() == b'raw media' for p in session.parts)
    saved = json.loads((session.root / 'session.json').read_text())
    if boundary in {'gate-rename', 'gate-fsync'}:
        assert not session.final.exists() and session.temporary.exists()
    else:
        assert session.final.exists() and gate.exists()
    assert uploader.publication_pending(session.final) == (boundary != 'gate-removal')
    if boundary == 'gate-removal':
        assert saved['publication_plan']['state'] == 'committed'
    else:
        assert saved['publication_plan']['state'] == 'pending'
        # Recover the exact saved plan, without invoking the media ladder again.
        monkeypatch.setattr(app, 'finalize_prepared_recording', Mock(side_effect=AssertionError('ladder forbidden')))
        session.engine._finalize_recording_worker(saved, 'retry durability boundary')
        assert saved['status'] == 'complete' and not session.root.exists()
        assert not gate.exists() and not uploader.publication_pending(session.final)
