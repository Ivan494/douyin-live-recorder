"""FLV timeline-reset auto-recovery (成熟女性 2026-11-12 incident)."""

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from recording_segments import _iter_tags, _read_flv_header, _write_tag, prepare_recording_parts
from tests.test_recording_segments import _flv, _generate_flv, _tool, _video_nalu, _video_seq


def _durations(manifest):
    return [float(line.split()[1]) for line in manifest.splitlines() if line.startswith("duration ")]


def test_zero_span_sliver_piece_is_dropped_not_fatal(tmp_path):
    frames = lambda start, stop: [(9, ts, _video_nalu(b"frame")) for ts in range(start, stop, 40)]
    capture = tmp_path / "part-0001.flv"
    capture.write_bytes(_flv(
        [(9, 0, _video_seq(b"one"))] + frames(0, 440)
        + [(9, 440, _video_seq(b"two")), (9, 440, _video_nalu(b"blip"))]  # sliver: 1 ms of media
        + [(9, 440, _video_seq(b"one"))] + frames(440, 880)))
    notes = []
    stable, manifest = prepare_recording_parts([capture], tmp_path / "work", notes)
    assert len(stable) == 2 and all("param-0002" not in piece.name for piece in stable)
    assert len(notes) == 1 and notes[0].startswith("sliver-drop: part-0001-param-0002.flv")
    assert _durations(manifest) == [0.44]


def test_non_advancing_real_pieces_fall_back_back_to_back(tmp_path):
    capture = tmp_path / "part-0001.flv"
    capture.write_bytes(_flv(
        [(9, 5000, _video_seq(b"one"))] + [(9, ts, _video_nalu(b"f")) for ts in range(5000, 7040, 40)]
        + [(9, 7040, _video_seq(b"two"))]
        # Audio first appears 3 s behind video: per-stream clocks are fine,
        # but this piece "starts" before the previous one.
        + [(8, ts, b"\xaf\x01aac") for ts in range(4000, 6000, 40)]
        + [(9, ts, _video_nalu(b"f")) for ts in range(7040, 9000, 40)]))
    notes = []
    stable, manifest = prepare_recording_parts([capture], tmp_path / "work", notes)
    assert len(stable) == 2
    assert any(note.startswith("back-to-back: part-0001.flv") for note in notes)
    assert _durations(manifest) == [2.04]  # piece 1's own span; last piece needs none


def _tags(path):
    with Path(path).open("rb") as handle:
        header = _read_flv_header(handle)
        return header, [tag for tag in _iter_tags(handle) if tag[0] != 18]


def _write(path, header, tags):
    with Path(path).open("wb") as handle:
        handle.write(header + b"\0" * 4)
        for tag in tags:
            _write_tag(handle, *tag)


def _engine(app, ffmpeg):
    class Store:
        settings = {"ffmpeg_path": ffmpeg, "ffprobe_path": ""}
    return app.MonitorEngine(Store(), app.queue.Queue())


def _recording(tmp_path, session, capture):
    return {"profile_id": "reset", "profile_name": "Reset", "final_output": str(tmp_path / "live.mkv"),
            "session_dir": str(session), "manifest_path": str(session / "session.json"),
            "stderr_path": str(tmp_path / "ffmpeg.log"), "parts": [str(capture)], "part_index": 1,
            "status": "recovering"}


def _probe_duration(ffprobe, path):
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True, check=True, timeout=30)
    return float(json.loads(result.stdout)["format"]["duration"])


def test_app_finalizer_recovers_real_media_sliver(tmp_path, caplog):
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required")
    import douyin_recorder_app as app
    from recording_segments import flv_playback_span_ms
    first, second = tmp_path / "a.flv", tmp_path / "b.flv"
    _generate_flv(ffmpeg, first, "320x240", "baseline")
    _generate_flv(ffmpeg, second, "640x360", "high")
    header, a_tags = _tags(first)
    _header, b_tags = _tags(second)
    shift = max(ts for kind, ts, _body in a_tags if kind == 8) + 23
    b_seq = next(tag for tag in b_tags if tag[0] == 9)
    b_key = next(tag for tag in b_tags if tag[0] == 9 and tag[2][1] == 1)
    replay = [(kind, ts + shift + (1 if kind == 9 and body[1] == 1 else 0), body) for kind, ts, body in a_tags]
    session = tmp_path / "session"
    session.mkdir()
    capture = session / "part-0001.flv"
    _write(capture, header, a_tags + [(9, shift, b_seq[2]), (9, shift, b_key[2])] + replay)
    original = capture.read_bytes()
    expected = flv_playback_span_ms(capture) / 1000
    recording = _recording(tmp_path, session, capture)
    with caplog.at_level(logging.WARNING), patch.object(app, "resolve_ffmpeg_executable", return_value=ffmpeg):
        _engine(app, ffmpeg)._finalize_recording_worker(recording, "sliver fixture")
    assert recording["status"] == "complete", recording.get("stop_reason")
    assert "FLV timeline sliver dropped" in caplog.text
    assert abs(_probe_duration(ffprobe, recording["final_outputs"][0]) - expected) <= 2.0
    assert len(original) > 0


def test_app_finalizer_recovers_genuine_clock_reset(tmp_path):
    # Was a FINALIZE_FAILED case before split-at-reset (2026-11-12 owner GO).
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required")
    import douyin_recorder_app as app
    source = tmp_path / "a.flv"
    _generate_flv(ffmpeg, source, "320x240", "baseline")
    header, tags = _tags(source)
    session = tmp_path / "session"
    session.mkdir()
    capture = session / "part-0001.flv"
    _write(capture, header, tags + tags)  # the stream clock restarts from 0
    original = capture.read_bytes()
    recording = _recording(tmp_path, session, capture)
    with patch.object(app, "resolve_ffmpeg_executable", return_value=ffmpeg):
        _engine(app, ffmpeg)._finalize_recording_worker(recording, "reset fixture")
    assert recording["status"] == "complete", recording.get("stop_reason")
    assert Path(recording["final_outputs"][0]).stat().st_size > 0
    assert len(original) > 0  # raw read only; normal publish then cleans the session


@pytest.mark.parametrize("total,verdict", [(7709.752, "PASS"), (7600.0, "FAIL")])
def test_recovered_duration_must_match_capture_spans(tmp_path, monkeypatch, total, verdict):
    import douyin_recorder_app as app
    engine = object.__new__(app.MonitorEngine)
    monkeypatch.setattr(engine, "_validate_final_recording", lambda _path, expected=None: total, raising=False)
    log = tmp_path / "finalize.log"
    if verdict == "PASS":
        engine._verify_recovered_duration([tmp_path / "x.mkv"], 7709.662, log)
    else:
        with pytest.raises(RuntimeError, match="raw capture must be preserved"):
            engine._verify_recovered_duration([tmp_path / "x.mkv"], 7709.662, log)
    assert f"RECOVERY VERIFY {verdict}" in log.read_text(encoding="utf-8")


def test_headless_does_not_republish_an_uploaded_failed_session(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("DOUYIN_HEADLESS", "1")
    import douyin_headless as h
    part = tmp_path / "part-0001.flv"
    part.write_bytes(b"x")
    ledger = tmp_path / "uploaded.jsonl"
    ledger.write_text(json.dumps({"path": "/d/x/2026-11-12_13-36-21_A_b.mkv",
                                  "drive_path": "saridq:Douyin/x/lives/2026-11-12_13-36-21_A_b.mkv"}) + "\nnot json\n",
                      encoding="utf-8")
    monkeypatch.setattr(h.HeadlessMonitorEngine, "UPLOADED_LEDGER", ledger)
    retry = h.HeadlessMonitorEngine._should_retry_finalize
    assert not retry({"status": "failed", "parts": [str(part)], "final_output": "/d/x/2026-11-12_13-36-21_A_b.mkv"})
    assert retry({"status": "failed", "parts": [str(part)], "final_output": "/d/x/other.mkv"})


# ---- split-at-reset: a capture whose own clock goes backwards mid-part ----

def _gen(ffmpeg, path, size, codec="libx264"):
    """Same fixtures as test_recording_segments (proven to splice cleanly)."""
    if codec == "libx264":
        _generate_flv(ffmpeg, path, size, "high" if size == "640x360" else "baseline")
        return path
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
         "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "1",
         "-c:v", codec, "-preset", "ultrafast", "-g", "25", "-bf", "0",
         "-x265-params", "log-level=error", "-c:a", "aac", "-f", "flv", str(path)],
        capture_output=True, check=True, timeout=60)
    return path


def _finalize_reset_capture(tmp_path, ffmpeg, sources):
    """Join ``sources`` tag-for-tag: every source restarts the clock near 0."""
    import douyin_recorder_app as app
    from recording_segments import TimestampDiscontinuity, flv_playback_span_ms
    header, tags = None, []
    for source in sources:
        source_header, source_tags = _tags(source)
        header = header or source_header
        tags += source_tags
    session = tmp_path / "session"
    session.mkdir()
    capture = session / "part-0001.flv"
    _write(capture, header, tags)
    with pytest.raises(TimestampDiscontinuity):
        flv_playback_span_ms(capture)  # raw clock alone is not authoritative
    recording = _recording(tmp_path, session, capture)
    original = capture.read_bytes()
    with patch.object(app, "resolve_ffmpeg_executable", return_value=ffmpeg), \
            patch.object(app.shutil, "rmtree", lambda *_a, **_k: None):  # keep finalize.log to inspect
        _engine(app, ffmpeg)._finalize_recording_worker(recording, "reset fixture")
    return app, recording, session, capture, original


@pytest.mark.parametrize("case", ["one_reset", "two_resets", "reset_and_sps_change", "hevc_reset_and_size_change"])
def test_clock_reset_mid_part_is_split_and_plays_end_to_end(tmp_path, case):
    from recording_segments import flv_playback_span_ms
    from tests.test_recording_segments import _assert_silent_full_decode, _frame_fingerprints, _splice_flv
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required")
    a = _gen(ffmpeg, tmp_path / "a.flv", "320x240")
    b = _gen(ffmpeg, tmp_path / "b.flv", "320x240")
    if case == "one_reset":
        sources, originals = [a, b], [a, b]
    elif case == "two_resets":
        c = _gen(ffmpeg, tmp_path / "c.flv", "320x240")
        sources, originals = [a, b, c], [a, b, c]
    elif case == "reset_and_sps_change":
        high = _gen(ffmpeg, tmp_path / "high.flv", "640x360")
        spliced = tmp_path / "spliced.flv"
        _splice_flv(a, high, spliced)  # parameter-set change inside the first clock
        sources, originals = [spliced, b], [a, high, b]
    else:
        a = _gen(ffmpeg, tmp_path / "ha.flv", "320x240", "libx265")
        b = _gen(ffmpeg, tmp_path / "hb.flv", "640x360", "libx265")
        sources, originals = [a, b], [a, b]
    expected_frames = [frame for path in originals for frame in _frame_fingerprints(ffmpeg, path)]
    expected = sum(flv_playback_span_ms(path) for path in sources) / 1000
    app, recording, session, capture, original = _finalize_reset_capture(tmp_path, ffmpeg, sources)
    assert recording["status"] == "complete", recording.get("stop_reason")
    output = Path(recording["final_outputs"][0])
    log = (session / "finalize.log").read_text(encoding="utf-8")
    assert "RECOVERY split-at-reset: part-0001.flv" in log
    assert "RECOVERY VERIFY PASS" in log
    assert abs(_probe_duration(ffprobe, output) - expected) <= 0.25
    assert _frame_fingerprints(ffmpeg, output) == expected_frames
    _assert_silent_full_decode(ffmpeg, output)
    assert capture.read_bytes() == original


def test_clock_reset_with_corrupt_sub_piece_keeps_raw_and_fails(tmp_path):
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required")
    import douyin_recorder_app as app
    a = _gen(ffmpeg, tmp_path / "a.flv", "320x240")
    header, tags = _tags(a)
    session = tmp_path / "session"
    session.mkdir()
    capture = session / "part-0001.flv"
    # After the clock restarts, the stream switches to a malformed HEVC config.
    _write(capture, header, tags + [(9, 0, b"\x90hvc1bad-configuration"), (9, 0, b"\x91hvc1\0\0\0data"),
                                    (9, 40, b"\x91hvc1\0\0\0data")])
    original = capture.read_bytes()
    recording = _recording(tmp_path, session, capture)
    with patch.object(app, "resolve_ffmpeg_executable", return_value=ffmpeg):
        _engine(app, ffmpeg)._finalize_recording_worker(recording, "corrupt reset fixture")
    assert recording["status"] == "failed"
    assert capture.read_bytes() == original
    assert not (tmp_path / "live.mkv").exists()
    log = (session / "finalize.log").read_text(encoding="utf-8")
    assert "FINALIZE_FAILED" in log
    assert not (session / "parameter_sets").exists()  # work files only; raw stays


def test_clock_reset_duration_mismatch_keeps_raw_and_fails(tmp_path):
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required")
    import douyin_recorder_app as app
    real_span = app.flv_playback_span_ms
    a = _gen(ffmpeg, tmp_path / "a.flv", "320x240")
    b = _gen(ffmpeg, tmp_path / "b.flv", "320x240")
    # The published file must cover the raw capture: inflate what it should cover.
    with patch.object(app, "flv_playback_span_ms",
                      lambda path: real_span(path) * (3 if Path(path).name == "part-0001.flv" else 1)):
        app_, recording, session, capture, original = _finalize_reset_capture(
            tmp_path, ffmpeg, [a, b])
    assert recording["status"] == "failed"
    assert capture.read_bytes() == original
    assert not (tmp_path / "live.mkv").exists()
    assert "FINALIZE_FAILED" in (session / "finalize.log").read_text(encoding="utf-8")


def test_split_at_reset_is_opt_in_for_unverified_callers(tmp_path):
    from recording_segments import TimestampDiscontinuity
    capture = tmp_path / "part-0001.flv"
    capture.write_bytes(_flv([(9, 0, _video_seq(b"one"))]
                             + [(9, ts, _video_nalu(b"f")) for ts in (0, 40, 80, 1000, 0, 40)]))
    with pytest.raises(TimestampDiscontinuity):
        prepare_recording_parts([capture], tmp_path / "work")
    notes = []
    stable, manifest = prepare_recording_parts([capture], tmp_path / "work2", notes, split_resets=True)
    assert [path.name for path in stable] == ["part-0001-epoch-0001.flv", "part-0001-epoch-0002.flv"]
    assert notes and notes[0].startswith("split-at-reset: part-0001.flv")
    assert _durations(manifest) == [1.04]
