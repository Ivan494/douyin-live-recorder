"""Sequence-header changes must survive capture and the final MKV publish."""

import collections
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from recording_segments import (
    _write_tag,
    prepare_recording_parts,
    ffmpeg_concat_copy_command,
    ffmpeg_live_capture_output_args,
    flv_playback_span_ms,
    split_flv_on_parameter_sets,
)


def _flv(tags, flags=0x05):
    buffer = bytearray(b"FLV\x01" + bytes((flags,)) + b"\x00\x00\x00\x09\x00\x00\x00\x00")
    class _Sink:
        def write(self, data):
            buffer.extend(data)

    sink = _Sink()
    for tag_type, timestamp, body in tags:
        _write_tag(sink, tag_type, timestamp, body)
    return bytes(buffer)


def _video_seq(payload):
    return bytes((0x17, 0, 0, 0, 0)) + payload


def _video_nalu(payload):
    return bytes((0x17, 1, 0, 0, 0)) + payload


def _audio_seq():
    return bytes((0xAF, 0, 0x12, 0x10))


def _sequence_headers(path):
    data = Path(path).read_bytes()
    assert data[:3] == b"FLV"
    pos = int.from_bytes(data[5:9], "big") + 4
    headers = []
    while pos + 11 <= len(data):
        tag_type = data[pos]
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        body = data[pos + 11:pos + 11 + size]
        if tag_type == 9 and len(body) > 5 and (body[0] & 0x0F) == 7 and body[1] == 0:
            headers.append(body)
        pos += 15 + size
    return headers


def test_unchanged_sequence_header_is_not_rewritten(tmp_path):
    source = tmp_path / "part-0001.flv"
    seq = _video_seq(b"sps-a")
    source.write_bytes(_flv([
        (9, 0, seq),
        (8, 0, _audio_seq()),
        (9, 40, _video_nalu(b"frame-1")),
        (9, 80, seq),
        (9, 120, _video_nalu(b"frame-2")),
    ]))

    pieces = split_flv_on_parameter_sets(source, tmp_path / "segments")

    assert pieces == [source]
    assert not (tmp_path / "segments").exists()


def test_sequence_header_change_splits_and_keeps_audio_config(tmp_path):
    source = tmp_path / "capture.flv"
    first = _video_seq(b"sps-a")
    second = _video_seq(b"sps-b-different")
    audio = _audio_seq()
    source.write_bytes(_flv([
        (18, 0, b"onMetaData"),
        (9, 0, first),
        (8, 0, audio),
        (9, 33, _video_nalu(b"gop-a")),
        (9, 2000, bytes((0x17, 2, 0, 0, 0))),
        (9, 2066, second),
        (9, 2100, _video_nalu(b"gop-b")),
    ]))
    legacy = tmp_path / "already.mkv"
    legacy.write_bytes(b"mkv")

    pieces, _manifest = prepare_recording_parts([source, legacy], tmp_path / "work")

    assert len(pieces) == 3
    assert pieces[0] != source
    assert pieces[2] == legacy
    assert _sequence_headers(pieces[0]) == [first]
    assert _sequence_headers(pieces[1]) == [second]
    assert audio in pieces[1].read_bytes()
    assert b"gop-a" in pieces[0].read_bytes()
    assert b"gop-b" in pieces[1].read_bytes()
    assert b"onMetaData" not in pieces[0].read_bytes()


def test_playback_span_ignores_a_single_boundary_gap(tmp_path):
    audio = [(8, timestamp, bytes((0xAF, 1, 1))) for timestamp in (0, 23, 46, 112)]
    video = [(9, timestamp, _video_nalu(b"v")) for timestamp in (0, 66)]
    path = tmp_path / "span.flv"
    path.write_bytes(_flv([(9, 0, _video_seq(b"sps")), *audio, *video]))

    # Usual audio step is 23 ms, so the packet at 112 ms ends at 135 ms,
    # not 178 ms from the one-off 66 ms jump.
    assert flv_playback_span_ms(path) == 135


def test_single_track_maps_live_in_the_capture_helper():
    """Map flags moved out of the app into the shared capture command."""
    source = Path(__file__).resolve().parents[1].joinpath("recording_segments.py").read_text(encoding="utf-8")
    capture = source.split("def ffmpeg_live_capture_output_args", 1)[1].split("def ffmpeg_concat_copy_command", 1)[0]
    publish = source.split("def ffmpeg_concat_copy_command", 1)[1].split("def concat_list_line", 1)[0]
    for block in (capture, publish):
        assert '"-map"' in block
        assert '"0:v:0?"' in block
        assert '"0:a:0?"' in block
        assert '"0:v?"' not in block
        assert '"copy"' in block


def test_enhanced_flv_sequence_start_also_splits(tmp_path):
    def enhanced(payload):
        # Keyframe + extended header + packet type 0 (sequence start) + fourcc.
        config = bytearray(23)
        config[0], config[21], config[22] = 1, 3, 3
        for kind in (32, 33, 34):
            nal = bytes((kind << 1, 1)) + payload
            config.extend(bytes((kind, 0, 1)) + len(nal).to_bytes(2, "big") + nal)
        return bytes((0x90,)) + b"hvc1" + config

    source = tmp_path / "hevc.flv"
    first = enhanced(b"vps-a")
    second = enhanced(b"vps-b")
    source.write_bytes(_flv([
        (9, 0, first),
        (9, 40, bytes((0x91,)) + b"hvc1" + b"slice"),
        (9, 80, second),
        (9, 120, bytes((0x91,)) + b"hvc1" + b"slice-b"),
    ]))

    pieces = split_flv_on_parameter_sets(source, tmp_path / "segments")

    assert len(pieces) == 2
    assert first in pieces[0].read_bytes()
    assert second not in pieces[0].read_bytes()
    assert second in pieces[1].read_bytes()


def _tool(name):
    configured = os.environ.get("DOUYIN_TEST_TOOLS_DIR")
    if configured:
        path = Path(configured) / (name + (".exe" if os.name == "nt" else ""))
        assert path.is_file(), f"Configured media test tool is missing: {path}"
        return str(path)
    bundled = Path(__file__).resolve().parents[1] / "tools" / f"{name}.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which(name)


def _packet_dts(ffprobe, path, stream):
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", stream,
            "-show_entries", "packet=dts_time", "-of", "csv=p=0", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    values = []
    for line in result.stdout.splitlines():
        token = line.split(",", 1)[0].strip()
        if token and token != "N/A":
            values.append(round(float(token), 6))
    return values


def _assert_silent_full_decode(ffmpeg, path):
    """The deploy gate: every stream, not only video, must decode with no errors."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def _decode_sizes(ffmpeg, path):
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(path),
            "-map", "0:v:0", "-vf", "showinfo", "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    sizes = re.findall(r"Parsed_showinfo.*?s:(\d+x\d+)", result.stderr)
    errors = re.findall(
        r"error while decoding MB|decode_slice_header error|Reference \d+|deblocking_filter_idc",
        result.stderr,
    )
    return collections.Counter(sizes), errors


def _generate_flv(ffmpeg, path, size, profile):
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-frames:v", "15",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", "-bf", "0",
            "-profile:v", profile,
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-f", "flv", str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _splice_flv(first, second, output):
    """Append a second FLV's tags after the first, keeping its sequence header."""
    def parse(path):
        data = Path(path).read_bytes()
        header_size = int.from_bytes(data[5:9], "big")
        pos = header_size + 4
        tags = []
        while pos + 11 <= len(data):
            tag_type = data[pos]
            size = int.from_bytes(data[pos + 1:pos + 4], "big")
            timestamp = int.from_bytes(data[pos + 4:pos + 7], "big") | (data[pos + 7] << 24)
            body = data[pos + 11:pos + 11 + size]
            if pos + 15 + size > len(data):
                break
            tags.append((tag_type, timestamp, body))
            pos += 15 + size
        return data[:header_size], tags

    header, first_tags = parse(first)
    _header, second_tags = parse(second)
    audio_ts = [
        timestamp for tag_type, timestamp, body in first_tags
        if tag_type == 8 and not (len(body) > 1 and body[1] == 0)
    ]
    steps = [later - earlier for earlier, later in zip(audio_ts, audio_ts[1:]) if later > earlier]
    frame_ms = max(set(steps), key=steps.count) if steps else 23
    last_audio = max(audio_ts) if audio_ts else max(timestamp for _tag, timestamp, _body in first_tags)
    second_origin = min(timestamp for tag_type, timestamp, _body in second_tags if tag_type in (8, 9))
    # Continue on the next audio frame so the fixture itself has no hole or overlap.
    shift = last_audio + frame_ms - second_origin
    buffer = bytearray(header + b"\x00\x00\x00\x00")

    class _Sink:
        def write(self, data):
            buffer.extend(data)

    sink = _Sink()
    for tag_type, timestamp, body in first_tags:
        _write_tag(sink, tag_type, timestamp, body)
    for tag_type, timestamp, body in second_tags:
        if tag_type == 18:
            continue
        _write_tag(sink, tag_type, timestamp + shift, body)
    Path(output).write_bytes(buffer)


def _publish_mkv(ffmpeg, parts, output, work_dir):
    stable, manifest = prepare_recording_parts(parts, work_dir)
    concat_path = Path(work_dir) / "parts.txt"
    concat_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path.write_text(manifest, encoding="utf-8")
    command = ffmpeg_concat_copy_command(ffmpeg, concat_path, output)
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return stable


def test_midstream_sps_change_publishes_a_fully_decodeable_mkv(tmp_path):
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    first = tmp_path / "baseline.flv"
    second = tmp_path / "high.flv"
    _generate_flv(ffmpeg, first, "320x240", "baseline")
    _generate_flv(ffmpeg, second, "640x360", "high")
    first_sizes, first_errors = _decode_sizes(ffmpeg, first)
    second_sizes, second_errors = _decode_sizes(ffmpeg, second)
    assert not first_errors and not second_errors
    assert first_sizes["320x240"] > 0
    assert second_sizes["640x360"] > 0

    spliced = tmp_path / "spliced.flv"
    _splice_flv(first, second, spliced)
    # Same stream copy the recorder uses for a live FLV pull.
    capture = tmp_path / "part-0001.flv"
    copied = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+discardcorrupt+genpts", "-i", str(spliced),
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", "-flush_packets", "1",
            "-f", "flv", str(capture),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert copied.returncode == 0, copied.stderr
    capture_sizes, capture_errors = _decode_sizes(ffmpeg, capture)
    assert not capture_errors
    assert capture_sizes["320x240"] == first_sizes["320x240"]
    assert capture_sizes["640x360"] == second_sizes["640x360"]

    naive = tmp_path / "naive.mkv"
    naive_copy = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(capture), "-c", "copy", str(naive)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert naive_copy.returncode == 0, naive_copy.stderr
    naive_sizes, naive_errors = _decode_sizes(ffmpeg, naive)
    assert naive_errors or naive_sizes["640x360"] < second_sizes["640x360"]

    published = tmp_path / "published.mkv"
    stable = _publish_mkv(ffmpeg, [capture], published, tmp_path / "parameter_sets")
    assert len(stable) >= 2
    published_sizes, published_errors = _decode_sizes(ffmpeg, published)
    assert not published_errors
    assert published_sizes["320x240"] == first_sizes["320x240"]
    assert published_sizes["640x360"] == second_sizes["640x360"]
    _assert_silent_full_decode(ffmpeg, published)

    # A later lossless remux must keep both parameter sets.
    recopy = tmp_path / "recopy.mkv"
    again = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(published), "-c", "copy", str(recopy)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    recopy_sizes, recopy_errors = _decode_sizes(ffmpeg, recopy)
    assert not recopy_errors
    assert recopy_sizes == published_sizes


def test_app_finalizer_publishes_decodeable_mkv_after_sps_change(tmp_path):
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    import douyin_recorder_app as app

    first = tmp_path / "baseline.flv"
    second = tmp_path / "high.flv"
    _generate_flv(ffmpeg, first, "320x240", "baseline")
    _generate_flv(ffmpeg, second, "640x360", "high")
    first_sizes, _first_errors = _decode_sizes(ffmpeg, first)
    second_sizes, _second_errors = _decode_sizes(ffmpeg, second)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    capture = session_dir / "part-0001.flv"
    spliced = tmp_path / "spliced.flv"
    _splice_flv(first, second, spliced)
    copied = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(spliced), *app.ffmpeg_live_capture_output_args(capture)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert copied.returncode == 0, copied.stderr
    final_output = tmp_path / "live.mkv"

    class Store:
        settings = {"ffmpeg_path": ffmpeg, "ffprobe_path": ""}

    engine = app.MonitorEngine(Store(), app.queue.Queue())
    recording = {
        "profile_id": "pk",
        "profile_name": "PK",
        "final_output": str(final_output),
        "session_dir": str(session_dir),
        "manifest_path": str(session_dir / "session.json"),
        "stderr_path": str(tmp_path / "ffmpeg.log"),
        "started_at": time.time(),
        "parts": [str(capture)],
        "part_index": 1,
        "status": "recovering",
    }
    with patch.object(app, "resolve_ffmpeg_executable", return_value=ffmpeg):
        engine._finalize_recording_worker(recording, "sps test")

    assert recording["status"] == "complete", recording.get("stop_reason")
    assert final_output.exists()
    assert not session_dir.exists()
    sizes, errors = _decode_sizes(ffmpeg, final_output)
    assert not errors
    assert sizes["320x240"] == first_sizes["320x240"]
    assert sizes["640x360"] == second_sizes["640x360"]
    _assert_silent_full_decode(ffmpeg, final_output)


def test_app_finalizer_still_joins_legacy_mkv_parts(tmp_path):
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    import douyin_recorder_app as app

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    parts = []
    for index, color in enumerate(("red", "blue"), start=1):
        part = session_dir / f"part-{index:04d}.mkv"
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=10",
                "-frames:v", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(part),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        parts.append(str(part))
    final_output = tmp_path / "legacy.mkv"

    class Store:
        settings = {"ffmpeg_path": ffmpeg, "ffprobe_path": ""}

    engine = app.MonitorEngine(Store(), app.queue.Queue())
    recording = {
        "profile_id": "legacy",
        "final_output": str(final_output),
        "session_dir": str(session_dir),
        "manifest_path": str(session_dir / "session.json"),
        "started_at": time.time(),
        "parts": parts,
        "status": "recovering",
    }
    with patch.object(app, "resolve_ffmpeg_executable", return_value=ffmpeg):
        engine._finalize_recording_worker(recording, "legacy mkv")

    assert recording["status"] == "complete", recording.get("stop_reason")
    sizes, errors = _decode_sizes(ffmpeg, final_output)
    assert not errors
    assert sizes["160x120"] == 10


def test_watcher_records_a_capture_flv_before_publishing_mkv(tmp_path, monkeypatch):
    import douyin_live_watcher as watcher

    seen = {}

    def fake_popen(cmd, **_kwargs):
        seen["cmd"] = cmd

        class Process:
            returncode = 0

            def poll(self):
                return 0

        return Process()

    monkeypatch.setattr(watcher, "resolve_trusted_executable", lambda *_args, **_kwargs: "ffmpeg.exe")
    monkeypatch.setattr(watcher.subprocess, "Popen", fake_popen)
    stream = SimpleNamespace(
        anchor_name="Anchor",
        title="Title",
        flv_url="https://cdn.example/live.flv",
        record_url="",
        m3u8_url="",
    )
    watcher.run_ffmpeg(
        {"output_dir": str(tmp_path), "container": "mkv", "ffmpeg_path": "ffmpeg.exe"},
        stream,
        logging.getLogger("watcher-test"),
    )

    command = seen["cmd"]
    assert command[-1].endswith(".capture.flv")
    assert command[command.index("-i") + 1] == "https://cdn.example/live.flv"
    assert command.index("-f") > command.index("-i")
    assert command[command.index("-f") + 1] == "flv"
    assert "-c" in command and command[command.index("-c") + 1] == "copy"


def test_two_header_changes_decode_every_stream_without_errors(tmp_path):
    """Two SPS changes (three parameter sets) must not overlap or drop audio."""
    ffmpeg = _tool("ffmpeg")
    ffprobe = _tool("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required")
    pieces = [
        (tmp_path / "baseline.flv", "320x240", "baseline"),
        (tmp_path / "high.flv", "640x360", "high"),
        (tmp_path / "main.flv", "480x270", "main"),
    ]
    for path, size, profile in pieces:
        _generate_flv(ffmpeg, path, size, profile)
    spliced = tmp_path / "spliced.flv"
    _splice_flv(pieces[0][0], pieces[1][0], tmp_path / "ab.flv")
    _splice_flv(tmp_path / "ab.flv", pieces[2][0], spliced)
    capture = tmp_path / "part-0001.flv"
    copied = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+discardcorrupt+genpts", "-i", str(spliced),
            *ffmpeg_live_capture_output_args(capture),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    assert copied.returncode == 0, copied.stderr
    published = tmp_path / "two-switches.mkv"
    stable = _publish_mkv(ffmpeg, [capture], published, tmp_path / "parameter_sets")
    assert len(stable) >= 3
    _assert_silent_full_decode(ffmpeg, published)
    for stream in ("a", "v"):
        assert _packet_dts(ffprobe, published, stream) == _packet_dts(ffprobe, capture, stream)
    sizes, errors = _decode_sizes(ffmpeg, published)
    assert not errors
    assert sizes["320x240"] > 0
    assert sizes["640x360"] > 0
    assert sizes["480x270"] > 0
    # The joined timeline keeps each AAC frame's own spacing: no shared DTS
    # and no hole larger than one extra frame beyond the source.
    audio_dts = _packet_dts(ffprobe, published, "a")
    deltas = [round(later - earlier, 6) for earlier, later in zip(audio_dts, audio_dts[1:])]
    assert deltas
    # One AAC frame is 1024/44100 s (~23 ms). A shared DTS is an overlap;
    # anything above one frame is a hole at a parameter-set boundary.
    assert min(deltas) > 0
    assert max(deltas) <= 0.024


def test_stable_flv_capture_still_publishes_every_frame(tmp_path):
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "stable.flv"
    _generate_flv(ffmpeg, source, "160x120", "baseline")
    before, before_errors = _decode_sizes(ffmpeg, source)
    assert not before_errors and before["160x120"] > 0
    assert split_flv_on_parameter_sets(source, tmp_path / "unused") == [source]

    published = tmp_path / "stable.mkv"
    _publish_mkv(ffmpeg, [source], published, tmp_path / "parameter_sets")
    after, after_errors = _decode_sizes(ffmpeg, published)
    assert not after_errors
    assert after["160x120"] == before["160x120"]
    _assert_silent_full_decode(ffmpeg, published)


def _frame_fingerprints(ffmpeg, path):
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-v', 'info', '-i', str(path),
         '-map', '0:v:0', '-vf', 'showinfo', '-f', 'null', '-'],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return re.findall(r'Parsed_showinfo.*?s:(\d+x\d+).*?checksum:([0-9A-F]+)', result.stderr)


def _packet_pts(ffprobe, path, stream):
    import json
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', stream, '-show_packets',
         '-show_entries', 'packet=pts_time', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return [p['pts_time'] for p in json.loads(result.stdout)['packets']]


@pytest.mark.parametrize('codec', ['libx264', 'libx265'])
@pytest.mark.parametrize('bframes', [0, 2])
def test_parameter_changes_preserve_pixels_and_timeline(tmp_path, codec, bframes):
    from recording_segments import _read_flv_header, _iter_tags, _video_sequence_header
    ffmpeg, ffprobe = _tool('ffmpeg'), _tool('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('ffmpeg and ffprobe are required')
    originals = []
    for index, size in enumerate(('320x240', '640x360')):
        path = tmp_path / f'original-{index}.flv'
        subprocess.run(
            [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=25',
             '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '1',
             '-c:v', codec, '-preset', 'ultrafast', '-g', '25', '-bf', str(bframes),
             *(['-x265-params', 'log-level=error'] if codec == 'libx265' else []),
             '-c:a', 'aac', '-f', 'flv', str(path)],
            capture_output=True, check=True, timeout=60,
        )
        originals.append(path)
    spliced = tmp_path / 'spliced.flv'
    _splice_flv(*originals, spliced)
    # Real timestamp gaps must survive splitting, including with reordered video.
    with spliced.open('rb') as handle:
        header = _read_flv_header(handle)
        tags = list(_iter_tags(handle))
    previous, changed = None, False
    with spliced.open('wb') as handle:
        handle.write(header + b'\0' * 4)
        for kind, timestamp, body in tags:
            seq = _video_sequence_header(body) if kind == 9 else None
            if seq is not None:
                changed |= previous is not None and seq != previous
                previous = seq
            _write_tag(handle, kind, timestamp + (47 if changed else 0), body)
    capture = tmp_path / 'capture.flv'
    subprocess.run(
        [ffmpeg, '-v', 'error', '-y', '-i', str(spliced), *ffmpeg_live_capture_output_args(capture)],
        capture_output=True, check=True, timeout=60,
    )
    published = tmp_path / 'published.mkv'
    _publish_mkv(ffmpeg, [capture], published, tmp_path / 'work')
    expected = [frame for path in originals for frame in _frame_fingerprints(ffmpeg, path)]
    assert len(expected) == 50
    assert _frame_fingerprints(ffmpeg, capture) == expected
    assert _frame_fingerprints(ffmpeg, published) == expected
    for stream in ('v', 'a'):
        assert _packet_pts(ffprobe, published, stream) == _packet_pts(ffprobe, capture, stream)
    assert _packet_dts(ffprobe, published, 'a') == _packet_dts(ffprobe, capture, 'a')
    _assert_silent_full_decode(ffmpeg, published)


@pytest.mark.parametrize('failure', ['malformed_hevc', 'remux_failure'])
def test_failed_finalization_preserves_original_capture(tmp_path, monkeypatch, failure):
    import douyin_recorder_app as app
    ffmpeg = _tool('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg is required')
    session = tmp_path / 'session'
    session.mkdir()
    capture = session / 'part.flv'
    if failure == 'malformed_hevc':
        capture.write_bytes(_flv([(9, 0, b'\x90hvc1bad-configuration')]))
    else:
        _generate_flv(ffmpeg, capture, '160x120', 'baseline')
        monkeypatch.setattr(app.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=1))
    original = capture.read_bytes()
    output = tmp_path / 'out.mkv'
    engine = app.MonitorEngine(SimpleNamespace(settings={'ffmpeg_path': ffmpeg}), app.queue.Queue())
    monkeypatch.setattr(app, 'resolve_ffmpeg_executable', lambda _path: ffmpeg)
    recording = {'profile_id': 'failure', 'session_dir': str(session), 'parts': [str(capture)],
                 'final_output': str(output), 'manifest_path': str(session / 'session.json')}
    engine._finalize_recording_worker(recording, 'failure fixture')
    assert recording['status'] == 'failed'
    assert capture.read_bytes() == original
    assert not output.exists()


def test_header_only_tail_does_not_discard_finished_media(tmp_path):
    ffmpeg = _tool('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg is required')
    source = tmp_path / 'capture.flv'
    _generate_flv(ffmpeg, source, '160x120', 'baseline')
    expected = _frame_fingerprints(ffmpeg, source)
    with source.open('ab') as handle:
        _write_tag(handle, 9, 1500, _video_seq(b'new-but-no-media'))
        handle.write(b'\x09\0')  # Interrupted FLV tag at EOF.
    output = tmp_path / 'out.mkv'
    _publish_mkv(ffmpeg, [source], output, tmp_path / 'work')
    assert _frame_fingerprints(ffmpeg, output) == expected
    _assert_silent_full_decode(ffmpeg, output)


def test_watcher_remux_preserves_frames_and_removes_only_work_files(tmp_path):
    import douyin_live_watcher as watcher
    ffmpeg = _tool('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg is required')
    inputs = [tmp_path / 'a.flv', tmp_path / 'b.flv']
    for path, size in zip(inputs, ('160x120', '320x240')):
        _generate_flv(ffmpeg, path, size, 'baseline')
    expected = [frame for path in inputs for frame in _frame_fingerprints(ffmpeg, path)]
    capture = tmp_path / 'fixture.capture.flv'
    _splice_flv(*inputs, capture)
    output = tmp_path / 'fixture.mkv'
    with (tmp_path / 'remux.log').open('w', encoding='utf-8') as log:
        watcher._remux_capture(ffmpeg, capture, output, logging.getLogger('fixture'), log)
    assert _frame_fingerprints(ffmpeg, output) == expected
    _assert_silent_full_decode(ffmpeg, output)
    assert not capture.exists()
    assert not (tmp_path / 'fixture.capture.segments').exists()
    assert all(path.exists() for path in inputs)


def test_real_multi_track_input_publishes_one_video_and_audio(tmp_path):
    import json
    ffmpeg, ffprobe = _tool('ffmpeg'), _tool('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('ffmpeg and ffprobe are required')
    source = tmp_path / 'multiple.mkv'
    subprocess.run(
        [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=10',
         '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '0.5',
         '-map', '0:v', '-map', '1:a', '-map', '0:v', '-map', '1:a',
         '-c:v', 'libx264', '-bf', '0', '-c:a', 'aac', str(source)],
        capture_output=True, check=True, timeout=60,
    )
    capture = tmp_path / 'capture.flv'
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(source),
                    *ffmpeg_live_capture_output_args(capture)],
                   capture_output=True, check=True, timeout=60)
    for name, inputs in [('new', [capture]), ('legacy', [source])]:
        output = tmp_path / f'{name}.mkv'
        _publish_mkv(ffmpeg, inputs, output, tmp_path / name)
        result = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'stream=codec_type',
                                 '-of', 'json', str(output)],
                                capture_output=True, text=True, check=True, timeout=30)
        assert sorted(s['codec_type'] for s in json.loads(result.stdout)['streams']) == ['audio', 'video']
        assert len(_frame_fingerprints(ffmpeg, output)) == 5
        _assert_silent_full_decode(ffmpeg, output)
