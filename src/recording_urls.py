from urllib.parse import urlparse

# Microseconds. FFmpeg aborts a hung read after this window.
DEFAULT_RW_TIMEOUT_US = 30_000_000
# Seconds. Cap backoff while retrying a dropped live pull.
DEFAULT_RECONNECT_DELAY_MAX = 30


def _text_attr(stream, name):
    value = getattr(stream, name, "")
    if isinstance(value, str):
        return value.strip()
    return ""


def is_safe_recording_url(url):
    """Only allow http(s) stream URLs for FFmpeg -i inputs.

    Rejects file:, concat:, pipe:, and other protocols that could read local
    files or otherwise surprise the recorder if a stream URL is poisoned.
    """
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def _url_path(url):
    return urlparse(url).path.lower()


def _url_kind(url):
    path = _url_path(url)
    if path.endswith(".flv"):
        return "flv"
    if path.endswith(".m3u8"):
        return "hls"
    return "direct"


def _safe_candidate(url, kind):
    if is_safe_recording_url(url):
        return url, kind
    return "", ""


def recording_input_url(stream):
    flv_url = _text_attr(stream, "flv_url")
    if flv_url:
        return _safe_candidate(flv_url, "flv")

    record_url = _text_attr(stream, "record_url")
    if record_url:
        return _safe_candidate(record_url, _url_kind(record_url))

    m3u8_url = _text_attr(stream, "m3u8_url")
    if m3u8_url:
        return _safe_candidate(m3u8_url, "hls")

    return "", ""


def has_recording_url(stream):
    url, _kind = recording_input_url(stream)
    return bool(url)


def recording_extension(input_url, default_container):
    default_extension = str(default_container or "flv").lstrip(".") or "flv"
    return default_extension


def ffmpeg_live_input_options(
    input_url,
    *,
    rw_timeout_us=DEFAULT_RW_TIMEOUT_US,
    reconnect_delay_max=DEFAULT_RECONNECT_DELAY_MAX,
):
    """Input-side FFmpeg flags for live HTTP pulls.

    Enables reconnect for transient CDN/socket drops so one recording process
    can survive brief disconnects instead of exiting and opening a new file.

    Intentionally omits ``-reconnect_at_eof``: a true stream end should still
    make FFmpeg exit so the app can re-resolve the room and mark offline.
    """
    options = [
        "-rw_timeout",
        str(int(rw_timeout_us)),
        "-fflags",
        "+discardcorrupt+genpts",
    ]
    if is_safe_recording_url(input_url):
        # Keep the protocol surface narrow even if FFmpeg is invoked with a
        # surprising URL after a future caller bypasses recording_input_url.
        options.extend(
            [
                "-protocol_whitelist",
                "http,https,tcp,tls,crypto",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                str(int(reconnect_delay_max)),
            ]
        )
    return options
