"""Keep live H.264/HEVC parameter-set changes decodeable.

Douyin PK and co-stream segments often send a new AVC sequence header
(SPS/PPS) in the middle of one FLV pull. FFmpeg's FLV demuxer reports that
as packet side data. The Matroska muxer ignores it for H.264 ("Ignoring new
extradata") and keeps the first CodecPrivate, so later GOPs are decoded with
the wrong header.

The FLV muxer does write a fresh sequence header. Capture with ``-c copy``
into FLV, then split that file wherever the video sequence header bytes
change and concatenate the pieces. H.264 uses concat's automatic bitstream
filter; HEVC needs explicit in-band VPS/SPS/PPS. Both keep the source clock
across parameter changes and preserve the encoded media without re-encoding.
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
        "0:v:0?",
        "-map",
        "0:a:0?",
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
        "-xerror",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
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


def flv_playback_span_ms(path):
    """Milliseconds from the first media packet to the end of the last one.

    Returns None when ``path`` is not an FLV or has no media packets.
    """
    start, end = _flv_media_bounds_ms(path)
    return max(1, end - start) if start is not None else None


def _flv_media_bounds_ms(path):
    try:
        handle = Path(path).open("rb")
    except OSError:
        return None, None
    # Keep a histogram instead of millions of timestamps for long recordings.
    streams = {}
    with handle:
        if _read_flv_header(handle) is None:
            return None, None
        for tag_type, timestamp, body in _iter_tags(handle):
            if tag_type == 8 and not _is_audio_sequence_header(body):
                pts = timestamp
            elif tag_type == 9 and _is_video_media_packet(body):
                pts = timestamp
                if len(body) >= 5 and not body[0] & 0x80 and body[0] & 15 == 7:
                    pts += int.from_bytes(body[2:5], "big", signed=True)
                elif len(body) >= 8 and body[0] & 0x80 and body[0] & 15 == 1:
                    pts += int.from_bytes(body[5:8], "big", signed=True)
            else:
                continue
            state = streams.setdefault(tag_type, {"start": pts, "end": pts, "last": timestamp, "steps": {}})
            step = timestamp - state["last"]
            if step > 0:
                state["steps"][step] = state["steps"].get(step, 0) + 1
            state["last"] = timestamp
            state["start"] = min(state["start"], pts)
            state["end"] = max(state["end"], pts)
    ends = []
    starts = []
    for state in streams.values():
        counts = state["steps"]
        step = max(counts, key=lambda value: (counts[value], -value)) if counts else 1
        starts.append(state["start"])
        ends.append(state["end"] + step)
    if not ends:
        return None, None
    return min(starts), max(ends)


def prepare_recording_parts(parts, work_dir):
    """Return concat inputs and their manifest, retaining each capture's clock.

    Non-FLV parts (recordings already started as Matroska) are left as-is so
    an in-progress session from an older build can still be finalized.
    Pieces from ONE capture retain their original timestamps. Their actual
    start-time differences position them on that same timeline; estimating
    each piece's duration would remove gaps or overlap B-frames.
    Only separate reconnect captures are placed back to back.
    """
    sources = [Path(part) for part in parts]
    expanded = []
    lines = ["ffconcat version 1.0"]
    root = Path(work_dir)
    for source_index, part in enumerate(sources):
        pieces = split_flv_on_parameter_sets(part, root / f"{source_index:04d}-{part.stem}")
        if pieces == [part]:
            expanded.extend(pieces)
            lines.append(concat_list_line(part))
            if source_index < len(sources) - 1:
                span = flv_playback_span_ms(part)
                if span is not None:
                    lines.append(f"duration {span / 1000:.3f}")
            continue
        timed = [(piece, _flv_media_bounds_ms(piece)) for piece in pieces]
        # A final sequence header without media is not a concat input.
        timed = [(piece, bounds) for piece, bounds in timed if bounds[0] is not None]
        if not timed:
            raise ValueError("No media packets after preparing FLV capture")
        expanded.extend(piece for piece, _bounds in timed)
        for index, (piece, bounds) in enumerate(timed):
            lines.append(concat_list_line(piece))
            if index < len(timed) - 1:
                duration = timed[index + 1][1][0] - bounds[0]
                if duration <= 0:
                    raise ValueError("FLV timeline reset; raw capture must be preserved")
                lines.append(f"duration {duration / 1000:.3f}")
            elif source_index < len(sources) - 1:
                # The next independent capture starts after this whole input,
                # not after the duration of only its last parameter-set piece.
                end = _flv_media_bounds_ms(part)[1]
                lines.append(f"duration {(end - bounds[0]) / 1000:.3f}")
    return expanded, "\n".join(lines) + "\n"


def split_flv_on_parameter_sets(source, directory):
    """Split ``source`` into FLVs that each carry one video sequence header.

    Stable AVC and non-FLV files are returned unchanged. HEVC also needs
    its VPS/SPS/PPS in the first coded packet: concat's automatic bitstream
    conversion only handles H.264, so splitting alone silently misdecodes
    subsequent HEVC pieces. All media timestamps remain on the source clock.
    """
    source = Path(source)
    if not _flv_needs_preparation(source):
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
            hevc_prefix = b""
            index = 0

            def close_current():
                nonlocal current
                if current is not None:
                    current.close()
                    current = None

            def start_segment(seq_body=None, timestamp=0):
                nonlocal current, index
                close_current()
                index += 1
                path = directory / f"{source.stem}-param-{index:04d}.flv"
                current = path.open("wb")
                outputs.append(path)
                current.write(header)
                current.write((0).to_bytes(4, "big"))
                if seq_body is not None:
                    _write_tag(current, 9, timestamp, seq_body)
                if last_audio_seq is not None:
                    _write_tag(current, 8, 0, last_audio_seq)

            start_segment()
            for tag_type, timestamp, body in _iter_tags(src):
                if tag_type == 18 or (tag_type == 9 and _is_sequence_end(body)):
                    continue
                if tag_type == 8 and _is_audio_sequence_header(body):
                    # A new piece already carries the cached AAC config.
                    # Re-emitting the identical header creates needless new
                    # extradata and can corrupt FFmpeg's MKV header rewrite.
                    if body == last_audio_seq:
                        continue
                    last_audio_seq = body
                if tag_type == 9 and (seq := _video_sequence_header(body)) is not None:
                    if seq != current_seq:
                        if current_seq is not None:
                            if _video_codec(seq) != _video_codec(current_seq):
                                raise ValueError("Video codec changed; raw capture must be preserved")
                            if _video_codec(seq) not in (b"avc1", b"hvc1"):
                                raise ValueError("Unsupported codec change; raw capture must be preserved")
                            if (_video_codec(seq) == b"hvc1" and len(seq) > 26 and len(current_seq) > 26
                                    and (seq[26] & 3) != (current_seq[26] & 3)):
                                # MKV has one NAL length size for the entire track.
                                raise ValueError("HEVC NAL length size changed; raw capture must be preserved")
                            start_segment(body, timestamp)
                        else:
                            _write_tag(current, tag_type, timestamp, body)
                        current_seq = seq
                        hevc_prefix = _hevc_parameter_prefix(seq)
                    continue
                if tag_type == 9 and hevc_prefix:
                    offset = _hevc_coded_payload_offset(body)
                    if offset is not None:
                        body = body[:offset] + hevc_prefix + body[offset:]
                        hevc_prefix = b""
                _write_tag(current, tag_type, timestamp, body)
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
        "Prepared %s as %s parameter-stable FLV segment(s).",
        source.name,
        len(written),
    )
    return written


def _flv_needs_preparation(path):
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
            if _video_codec(seq) == b"hvc1":
                return True
            if first is None:
                first = seq
            elif seq != first:
                return True
    return False


def _video_codec(sequence_header):
    return sequence_header[1:5] if sequence_header[0] & 0x80 else b"avc1"


def _hevc_parameter_prefix(sequence_header):
    """Copy hvcC parameter NALs into length-prefixed packet form (no encoding)."""
    if _video_codec(sequence_header) != b"hvc1":
        return b""
    config = sequence_header[5:]
    if len(config) < 23 or config[0] != 1:
        raise ValueError("Invalid HEVC configuration; raw capture must be preserved")
    length_size = (config[21] & 3) + 1
    offset = 23
    prefix = bytearray()
    types = set()
    for _ in range(config[22]):
        if offset + 3 > len(config):
            raise ValueError("Truncated HEVC parameter array")
        nal_type = config[offset] & 0x3F
        count = int.from_bytes(config[offset + 1:offset + 3], "big")
        offset += 3
        for _ in range(count):
            if offset + 2 > len(config):
                raise ValueError("Truncated HEVC parameter length")
            size = int.from_bytes(config[offset:offset + 2], "big")
            offset += 2
            if size < 2 or offset + size > len(config) or size >= 1 << (8 * length_size):
                raise ValueError("Invalid HEVC parameter size")
            if nal_type in (32, 33, 34):
                prefix.extend(size.to_bytes(length_size, "big"))
                prefix.extend(config[offset:offset + size])
                types.add(nal_type)
            offset += size
    if types != {32, 33, 34}:
        raise ValueError("Missing HEVC VPS/SPS/PPS; raw capture must be preserved")
    return bytes(prefix)


def _hevc_coded_payload_offset(body):
    if len(body) < 5 or not body[0] & 0x80 or body[1:5] != b"hvc1":
        return None
    packet_type = body[0] & 0x0F
    if packet_type == 1 and len(body) >= 8:  # CodedFrames, signed composition offset
        return 8
    if packet_type == 3:  # CodedFramesX, zero composition offset
        return 5
    return None


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


def _is_video_media_packet(body):
    if not body:
        return False
    if body[0] & 0x80:
        # Enhanced packet type 4 is metadata (e.g. color information), not a frame.
        return len(body) >= 5 and (body[0] & 15) in (1, 3)
    if body[0] & 15 == _AVC_CODEC_ID:
        return len(body) >= 5 and body[1] == 1
    return body[0] >> 4 != 5


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
