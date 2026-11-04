"""Offline DTS, duration and escalation regressions using real FFmpeg muxing."""
import json
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import douyin_recorder_app as app
import recording_segments as segments


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Network access is forbidden in finalizer tests")
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


@pytest.fixture
def capture(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg and FFprobe are required')
    session = tmp_path / 'session'
    session.mkdir()
    parts = [session / f'part-{i}.flv' for i in range(2)]
    for i, path in enumerate(parts):
        subprocess.run([
            ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            f'testsrc2=s={"160x120" if i == 0 else "320x240"}:r=25',
            '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '1',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '2',
            '-c:a', 'aac', str(path),
        ], capture_output=True, check=True, timeout=60)
    engine = app.MonitorEngine(SimpleNamespace(settings={
        'ffmpeg_path': ffmpeg, 'ffprobe_path': ffprobe}), app.RecorderEventQueue())
    monkeypatch.setattr(app, 'resolve_ffmpeg_executable', lambda _value: ffmpeg)
    monkeypatch.setattr(app, 'resolve_trusted_executable', lambda *_a, **_kw: ffprobe)
    output = tmp_path / 'capture.mkv'
    recording = {'profile_id': 'dts', 'session_dir': str(session),
                 'manifest_path': str(session / 'session.json'),
                 'final_output': str(output), 'parts': [str(p) for p in parts]}
    # A real overlap in packet clocks across the concat join, as seen with
    # reordered frames at reconnects/parameter changes. No FFmpeg mock here.
    manifest = ('ffconcat version 1.0\n' + segments.concat_list_line(parts[0]) +
                '\nduration 0.974\n' + segments.concat_list_line(parts[1]) + '\n')
    monkeypatch.setattr(app, 'prepare_recording_parts', lambda *_a: (parts, manifest))
    return SimpleNamespace(engine=engine, parts=parts, recording=recording,
                           output=output, session=session, manifest=manifest,
                           ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_backwards_dts_strict_fails_but_finalize_recovers_full_length(capture, tmp_path):
    manifest = tmp_path / 'strict.txt'
    manifest.write_text(capture.manifest)
    result = subprocess.run(segments.ffmpeg_concat_copy_command(
        capture.ffmpeg, manifest, tmp_path / 'strict.mkv'), capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert 'Non-monotonic DTS' in result.stderr
    # Keep the input durations for checking after successful raw cleanup.
    expected = sum(segments.flv_playback_span_ms(p) / 1000 for p in capture.parts)
    capture.engine._finalize_recording_worker(capture.recording, 'overlap')
    assert capture.recording['status'] == 'complete'
    segments.probe_recording(capture.output, capture.ffprobe, expected)
    assert not capture.session.exists()


@pytest.mark.parametrize('split', [False, True])
def test_normalize_and_sequential_fallback_publish_all_media(capture, monkeypatch, split):
    original_run = subprocess.run
    def run(command, **kwargs):
        if command[0] == capture.ffmpeg:
            manifest = Path(command[command.index('-i') + 1])
            if manifest.name == 'parts.txt' or (split and manifest.name == 'normalized.txt'):
                return subprocess.CompletedProcess(command, 234)
        return original_run(command, **kwargs)
    monkeypatch.setattr(segments.subprocess, 'run', run)
    expected = sum(segments.flv_playback_span_ms(p) / 1000 for p in capture.parts)
    capture.engine._finalize_recording_worker(capture.recording, 'forced concat failure')
    assert capture.recording['status'] == 'complete', capture.recording.get('stop_reason')
    outputs = [Path(p) for p in capture.recording['final_outputs']]
    assert len(outputs) == (2 if split else 1)
    if split:
        assert [p.name for p in outputs] == ['capture.part01.mkv', 'capture.part02.mkv']
        assert not capture.output.exists()
    durations = [segments.probe_recording(p, capture.ffprobe) for p in outputs]
    segments.validate_recording_duration(sum(durations), expected)
    for path in outputs:
        result = original_run([capture.ffmpeg, '-v', 'error', '-i', str(path), '-f', 'null', '-'],
                              capture_output=True, text=True, timeout=60)
        assert result.returncode == 0 and not result.stderr, result.stderr
    assert not capture.session.exists()
    assert not list(capture.output.parent.rglob('*.finalizing.mkv'))


def test_short_output_is_rejected_by_real_probe(capture):
    with pytest.raises(RuntimeError, match='differs from expected'):
        segments.probe_recording(capture.parts[0], capture.ffprobe, expected=10)


@pytest.mark.parametrize('failure', ['timeout', 'short-output', 'bad-segment'])
def test_all_strategies_fail_preserving_raw_and_appending_logs(capture, monkeypatch, caplog, failure):
    original_run = subprocess.run
    originals = [p.read_bytes() for p in capture.parts]
    def run(command, **kwargs):
        if command[0] == capture.ffmpeg:
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 1)
            if failure == 'bad-segment':
                manifest = Path(command[command.index('-i') + 1])
                # First fallback segment works; the second must prevent any
                # publication, rather than publish a shortened recording.
                if manifest.name == 'single.txt' and str(capture.parts[0]) in manifest.read_text():
                    return original_run(command, **kwargs)
                return subprocess.CompletedProcess(command, 234)
            shutil.copyfile(capture.parts[0], command[-1])
            return subprocess.CompletedProcess(command, 0)
        return original_run(command, **kwargs)
    monkeypatch.setattr(segments.subprocess, 'run', run)
    if failure == 'short-output':
        # Shorten even per-segment normalizations; no valid duration may pass.
        original_validate = capture.engine._validate_final_recording
        def validate(path, expected=None):
            return original_validate(path, expected * 10 if expected else None)
        monkeypatch.setattr(capture.engine, '_validate_final_recording', validate)
    for attempt in (1, 2):
        capture.engine._finalize_recording_worker(capture.recording, 'failure regression')
        assert capture.recording['status'] == 'failed'
        assert capture.recording['finalize_attempts'] == attempt
        assert [p.read_bytes() for p in capture.parts] == originals
        assert not capture.output.exists()
        assert not list(capture.output.parent.glob('*.part*.mkv'))
    log = (capture.session / 'finalize.log').read_text()
    assert log.count('=== finalize call') == 2
    assert 'FINALIZE_FAILED' in log and str(capture.session) in log
    assert 'FINALIZE_FAILED' in caplog.text
    assert not (capture.session / 'finalize_remux').exists()
    assert json.loads((capture.session / 'session.json').read_text())['status'] == 'failed'
