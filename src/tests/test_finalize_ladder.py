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


@pytest.mark.parametrize('clock_damage', ['reset', 'wrap'])
def test_timestamp_epochs_cannot_authorize_short_output_cleanup(capture, monkeypatch, clock_damage):
    # Exercise real preparation, media tools, and worker cleanup together.
    monkeypatch.setattr(app, 'prepare_recording_parts', segments.prepare_recording_parts)
    source = capture.parts[0]
    expected = segments.flv_playback_span_ms(source) / 1000
    with source.open('rb') as handle:
        header = segments._read_flv_header(handle)
        tags = list(segments._iter_tags(handle))
    with source.open('wb') as handle:
        handle.write(header + b'\0' * 4)
        for _ in range(2 if clock_damage == 'reset' else 1):
            for kind, timestamp, body in tags:
                if kind == 18 or (kind == 9 and segments._is_sequence_end(body)):
                    continue
                shift = (1 << 32) - 400 if clock_damage == 'wrap' else 0
                segments._write_tag(handle, kind, timestamp + shift, body)
    original = source.read_bytes()
    capture.recording['parts'] = [str(source)]
    capture.parts[1].unlink()
    if clock_damage == 'wrap':
        assert segments.flv_playback_span_ms(source) / 1000 == pytest.approx(expected)
    else:
        with pytest.raises(segments.TimestampDiscontinuity, match='not authoritative'):
            segments.flv_playback_span_ms(source)
    capture.engine._finalize_recording_worker(capture.recording, clock_damage)
    if capture.recording['status'] == 'complete':
        assert clock_damage == 'wrap'
        outputs = [Path(p) for p in capture.recording['final_outputs']]
        segments.validate_recording_duration(sum(segments.probe_recording(p, capture.ffprobe)
                                                for p in outputs), expected)
    else:
        assert capture.recording['status'] == 'failed'
        assert source.read_bytes() == original
        assert not capture.output.exists()
        assert not list(capture.output.parent.glob('*.part*.mkv'))


def test_many_normalizations_share_one_deadline_and_input_allowances(tmp_path, monkeypatch):
    import io
    inputs = [tmp_path / f'in{i}.flv' for i in range(20)]
    for path in inputs:
        path.write_bytes(b'input')
    # Sparse file: a session-sized allowance differs substantially from the
    # tiny subsequent normalization inputs without allocating gigabytes.
    with inputs[0].open('r+b') as handle:
        handle.truncate(10 << 30)
    now, calls = [0.0], []
    monkeypatch.setattr(segments.time, 'monotonic', lambda: now[0])
    def run(command, **kwargs):
        allowance = kwargs['timeout']
        output = Path(command[-1])
        calls.append((output.name, allowance))
        if not output.name.startswith('part'):
            now[0] += 5
            return subprocess.CompletedProcess(command, 234)
        elapsed = min(29, allowance)
        now[0] += elapsed
        if elapsed < 29:
            raise subprocess.TimeoutExpired(command, allowance)
        output.write_bytes(b'verified fixture')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(segments.subprocess, 'run', run)
    with segments.finalize_budget(100, base=30):
        with pytest.raises(segments.FinalizeDeadlineExceeded, match='deadline exhausted'):
            segments.finalize_prepared_recording('ffmpeg', inputs, tmp_path / 'inputs.txt',
                tmp_path / 'out.finalizing.mkv', tmp_path / 'remux', io.StringIO(),
                lambda _p, expected: expected, lambda _p: 1, lambda: len(inputs), timeout=100)
    assert now[0] == 100
    assert len(calls) == 6  # two failed concats, three normalizations, one timeout
    assert [cap for _name, cap in calls[:3]] == pytest.approx([100, 95, 90])
    assert all(30 <= cap < 31 for _name, cap in calls[3:-1])
    assert calls[-1][1] == pytest.approx(3)
    assert not (tmp_path / 'out.finalizing.mkv').exists()
    assert inputs[0].stat().st_size == 10 << 30
    assert all(p.read_bytes() == b'input' for p in inputs[1:])


def test_duration_and_preparation_scan_obey_deadline_and_share_cache(tmp_path, monkeypatch):
    path = tmp_path / 'input.flv'
    with path.open('wb') as handle:
        handle.write(b'FLV\x01\x01\0\0\0\x09' + b'\0' * 4)
        for timestamp in range(0, 2000, 40):
            segments._write_tag(handle, 9, timestamp, b'\x17\x01\0\0\0frame')
    original = segments._iter_tags
    visits, now = [], [0.0]
    monkeypatch.setattr(segments.time, 'monotonic', lambda: now[0])
    def counted(handle):
        visits.append(handle.name)
        yield from original(handle)
    monkeypatch.setattr(segments, '_iter_tags', counted)
    with segments.finalize_budget(60):
        segments.prepare_recording_parts([path], tmp_path / 'work')
        assert segments.flv_playback_span_ms(path) == 2000
        assert visits == [str(path)]
    def slow(handle):
        for tag in original(handle):
            now[0] += 1
            yield tag
    monkeypatch.setattr(segments, '_iter_tags', slow)
    with segments.finalize_budget(3):
        with pytest.raises(segments.FinalizeDeadlineExceeded):
            segments.flv_playback_span_ms(path)
    assert now[0] == 3


def test_probe_uses_remaining_budget_and_checks_after_process(tmp_path, monkeypatch):
    path = tmp_path / 'out.mkv'
    path.write_bytes(b'media')
    now, caps = [0.0], []
    monkeypatch.setattr(segments.time, 'monotonic', lambda: now[0])
    def probe(command, **kwargs):
        caps.append(kwargs['timeout'])
        now[0] += 2
        return subprocess.CompletedProcess(command, 0,
            '{"streams":[{"codec_type":"video"}],"format":{"duration":"1"}}', '')
    monkeypatch.setattr(segments.subprocess, 'run', probe)
    with segments.finalize_budget(2):
        with pytest.raises(segments.FinalizeDeadlineExceeded):
            segments.probe_recording(path, 'ffprobe', 1)
    assert caps == [2]
