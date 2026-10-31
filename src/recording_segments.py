"""Keep live H.264/HEVC parameter-set changes decodeable.

Douyin PK and co-stream segments often send a new AVC sequence header
(SPS/PPS) in the middle of one FLV pull. FFmpeg's FLV demuxer reports that
as packet side data. The Matroska muxer ignores it for H.264 ("Ignoring new
extradata") and keeps the first CodecPrivate, so later GOPs are decoded with
the wrong header.

The FLV muxer does write a fresh sequence header. Capture with ``-c copy``
into FLV, then split that file wherever the video sequence header bytes
change and concatenate the pieces. Each piece has one parameter set, and the
concat demuxer reinitializes the bitstream filter, so the joined MKV stores
both sets in-band and stays decodeable for the whole duration.
"""

from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)

# Classic FLV AVC codec id. Packet type 0 is the AVCDecoderConfigurationRecord.
_AVC_CODEC_ID = 7


def ffmpeg_live_capture_output_args(output_path):
    """Output-side FFmpeg args for one live capture file.

    FLV, not Matroska: a mid-stream sequence header must be stored, and the
    FLV muxer writes one when extradata changes. The path should end in
    ``.flv``; ``-f flv`` keeps the muxer from following a different suffix.
    """
    return [
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-flush_packets",
        "1",
        "-f",
        "flv",
        str(output_path),
    ]


def ffmpeg_concat_copy_command(ffmpeg_executable, concat_path, output_path):
    """Stream-copy concat demuxer command used to publish the finished MKV."""
    return [
        str(ffmpeg_executable),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "make_zero",
        str(output_path),
    ]


def concat_list_line(path):
    normalized = Path(path).resolve().as_posix().replace("'", "'\\''")
    return "file '" + normalized + "'"


def expand_recording_parts(parts, work_dir):
    """Return concat inputs, splitting any FLV whose sequence header changes.

    Non-FLV parts (recordings already started as Matroska) are left as-is so
    an in-progress session from an older build can still be finalized.
    """
    expanded = []
    root = Path(work_dir)
    for part in parts:
        part = Path(part)
        expanded.extend(split_flv_on_parameter_sets(part, root / part.stem))
    return expanded


def split_flv_on_parameter_sets(source, directory):
    """Split ``source`` into FLVs that each carry one video sequence header.

    A file that is not FLV, or whose header never changes, is returned
    unchanged. Timestamps inside a new piece are rebased to zero so the
    concat demuxer can place pieces back to back.
    """
    source = Path(source)
    if not _flv_has_parameter_change(source):
        return [source]

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    current = None
    try:
        with source.open("rb") as src:
            header = _read_flv_header(src)
            if header is None:
                return [source]
            current_seq = None
            last_audio_seq = None
            base = 0
            index = 0

            def close_current():
                nonlocal current
                if current is not None:
                    current.close()
                    current = None

            def start_segment(seq_body, timestamp):
                nonlocal current, base, index
                close_current()
                index += 1
                base = timestamp
                path = directory / f"{source.stem}-param-{index:04d}.flv"
                current = path.open("wb")
                outputs.append(path)
                current.write(header)
                current.write((0).to_bytes(4, "big"))
                _write_tag(current, 9, 0, seq_body)
                if last_audio_seq is not None:
                    _write_tag(current, 8, 0, last_audio_seq)

            for tag_type, timestamp, body in _iter_tags(src):
                if tag_type == 18 or (tag_type == 9 and _is_sequence_end(body)):
                    continue
                if tag_type == 8 and _is_audio_sequence_header(body):
                    last_audio_seq = body
                if tag_type == 9 and (seq := _video_sequence_header(body)) is not None:
                    if seq != current_seq:
                        current_seq = seq
                        start_segment(body, timestamp)
                    continue
                if current is None:
                    continue
                _write_tag(current, tag_type, timestamp - base, body)
        close_current()
    except Exception:
        if current is not None:
            current.close()
        for path in outputs:
            path.unlink(missing_ok=True)
        raise

    written = [path for path in outputs if path.exists() and path.stat().st_size > len(b"FLV")]
    if not written:
        return [source]
    logger.info(
        "Video sequence header changed inside %s; wrote %s parameter-stable FLV segment(s).",
        source.name,
        len(written),
    )
    return written


def _flv_has_parameter_change(path):
    try:
        handle = path.open("rb")
    except OSError:
        return False
    with handle:
        if _read_flv_header(handle) is None:
            return False
        first = None
        for tag_type, _timestamp, body in _iter_tags(handle):
            if tag_type != 9:
                continue
            seq = _video_sequence_header(body)
            if seq is None:
                continue
            if first is None:
                first = seq
            elif seq != first:
                return True
    return False


def _read_flv_header(handle):
    prefix = handle.read(9)
    if len(prefix) < 9 or prefix[:3] != b"FLV":
        return None
    header_size = int.from_bytes(prefix[5:9], "big")
    if header_size < 9:
        return None
    rest = handle.read(header_size - 9)
    if len(rest) != header_size - 9:
        return None
    if len(handle.read(4)) != 4:
        return None
    return prefix + rest


def _iter_tags(handle):
    while True:
        tag_header = handle.read(11)
        if not tag_header:
            return
        if len(tag_header) < 11:
            return
        size = int.from_bytes(tag_header[1:4], "big")
        timestamp = int.from_bytes(tag_header[4:7], "big") | (tag_header[7] << 24)
        body = handle.read(size)
        previous = handle.read(4)
        if len(body) != size or len(previous) != 4:
            return
        yield tag_header[0], timestamp, body


def _video_sequence_header(body):
    """Return the tag body when it is an AVC or enhanced-FLV sequence header."""
    if len(body) < 5:
        return None
    if body[0] & 0x80:
        # Enhanced RTMP: the low nibble is the packet type. 0 = sequence start.
        if (body[0] & 0x0F) == 0:
            return body
        return None
    if (body[0] & 0x0F) == _AVC_CODEC_ID and body[1] == 0:
        return body
    return None


def _is_sequence_end(body):
    if not body:
        return False
    if body[0] & 0x80:
        return (body[0] & 0x0F) == 2
    return len(body) > 1 and (body[0] & 0x0F) == _AVC_CODEC_ID and body[1] == 2


def _is_audio_sequence_header(body):
    # AAC sound format is 10. Packet type 0 is AudioSpecificConfig.
    return len(body) > 1 and (body[0] >> 4) == 10 and body[1] == 0


def _write_tag(handle, tag_type, timestamp, body):
    timestamp = max(0, int(timestamp)) & 0xFFFFFFFF
    size = len(body)
    handle.write(bytes((
        tag_type,
        (size >> 16) & 0xFF,
        (size >> 8) & 0xFF,
        size & 0xFF,
        (timestamp >> 16) & 0xFF,
        (timestamp >> 8) & 0xFF,
        timestamp & 0xFF,
        (timestamp >> 24) & 0xFF,
        0,
        0,
        0,
    )))
    handle.write(body)
    handle.write((11 + size).to_bytes(4, "big"))
