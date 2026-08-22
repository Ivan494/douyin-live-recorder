"""Shared URL, CDP, and executable validation for the recorder."""

from __future__ import annotations

import ipaddress
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Hosts allowed for Douyin share-link resolution (short links and landing pages).
_SHARE_LINK_SUFFIXES = (
    ".douyin.com",
    ".iesdouyin.com",
)

# Hosts allowed for media/CDN byte downloads.
_MEDIA_FETCH_SUFFIXES = _SHARE_LINK_SUFFIXES + (
    ".snssdk.com",
    ".amemv.com",
    ".byteimg.com",
    ".bytecdn.cn",
    ".ibytedtos.com",
    ".douyincdn.com",
    ".douyinpic.com",
    ".tiktokcdn.com",
    ".ixigua.com",
    ".pstatp.com",
)

_MEDIA_FETCH_EXACT = frozenset(
    {
        "douyin.com",
        "www.douyin.com",
        "v.douyin.com",
        "iesdouyin.com",
        "www.iesdouyin.com",
        "aweme.snssdk.com",
    }
)

_TRUSTED_TOOL_BASENAMES = frozenset(
    {
        "ffmpeg.exe",
        "ffprobe.exe",
        "yt-dlp.exe",
    }
)


def _normalize_host(host):
    return str(host or "").strip().lower().strip(".")


def is_loopback_host(host):
    normalized = _normalize_host(host)
    if not normalized:
        return False
    if normalized in _LOOPBACK_HOSTS:
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        return normalized[1:-1] in _LOOPBACK_HOSTS
    return False


def is_loopback_cdp_url(url):
    """Return True when a Chrome DevTools HTTP endpoint is loopback-only."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    return is_loopback_host(parsed.hostname)


def _hostname_resolves_to_blocked_ip(hostname):
    host = str(hostname or "").strip("[]")
    if not host:
        return True
    if is_loopback_host(host):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _host_matches_suffixes(host, suffixes):
    normalized = _normalize_host(host)
    if not normalized:
        return False
    return any(normalized == suffix[1:] or normalized.endswith(suffix) for suffix in suffixes)


def _host_allowed(host, *, exact_hosts, suffixes):
    normalized = _normalize_host(host)
    if not normalized:
        return False
    if normalized in exact_hosts:
        return True
    return _host_matches_suffixes(normalized, suffixes)


def is_safe_http_url(url, *, block_loopback=True):
    """Public http(s) URL with a hostname (used for FFmpeg stream inputs)."""
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if block_loopback and is_loopback_host(hostname):
        return False
    if _hostname_resolves_to_blocked_ip(hostname):
        return False
    return True


def is_safe_recording_url(url):
    """Alias kept for recording_urls and FFmpeg callers."""
    return is_safe_http_url(url)


def is_safe_share_link_url(url):
    """Allow Douyin short links and profile/video landing pages."""
    if not is_safe_http_url(url):
        return False
    parsed = urlparse(str(url or "").strip())
    return _host_allowed(parsed.hostname, exact_hosts=_MEDIA_FETCH_EXACT, suffixes=_SHARE_LINK_SUFFIXES)


def is_safe_media_download_url(url):
    """Allow Douyin CDN and API hosts for binary downloads."""
    if not is_safe_http_url(url):
        return False
    parsed = urlparse(str(url or "").strip())
    return _host_allowed(parsed.hostname, exact_hosts=_MEDIA_FETCH_EXACT, suffixes=_MEDIA_FETCH_SUFFIXES)


def follow_safe_redirects(client, url, *, url_validator, max_hops=10):
    """Follow redirects manually, validating every hop."""
    current = str(url or "").strip()
    if not current:
        raise ValueError("URL is empty")
    if not url_validator(current):
        raise ValueError(f"Refusing unsafe URL: {current}")

    response = client.get(current, follow_redirects=False)
    hops = 0
    while response.is_redirect and hops < max_hops:
        location = str(response.headers.get("location") or "").strip()
        if not location:
            break
        next_url = str(response.url.join(location))
        if not url_validator(next_url):
            raise ValueError(f"Refusing unsafe redirect target: {next_url}")
        current = next_url
        hops += 1
        response = client.get(current, follow_redirects=False)
    response.raise_for_status()
    return response


def resolve_trusted_executable(path_text, *, allowed_basenames=None, trusted_roots=()):
    """Resolve ffmpeg / yt-dlp paths to an existing file under trusted roots."""
    allowed = {name.lower() for name in (allowed_basenames or _TRUSTED_TOOL_BASENAMES)}
    raw = str(path_text or "").strip()
    if not raw:
        raise ValueError("Executable path is empty")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve(strict=False)

    # Accept PATH lookup when the basename is on the allow-list.
    which_hit = shutil.which(raw)
    if which_hit:
        which_path = Path(which_hit).resolve()
        if which_path.name.lower() in allowed and which_path.is_file():
            candidate = which_path

    if candidate.name.lower() not in allowed:
        raise ValueError(f"Executable must be one of: {', '.join(sorted(allowed))}")
    if not candidate.is_file():
        raise ValueError(f"Executable not found: {candidate}")

    resolved = candidate.resolve()
    roots = [Path(root).resolve() for root in trusted_roots if str(root or "").strip()]
    if roots:
        allowed_by_root = any(
            resolved == root or root in resolved.parents for root in roots
        )
        path_hit = shutil.which(resolved.name)
        path_allowed = bool(
            path_hit and Path(path_hit).resolve() == resolved
        )
        if not allowed_by_root and not path_allowed:
            raise ValueError(
                f"Executable must live under a trusted tools directory or PATH: {resolved}"
            )
    return str(resolved)


def default_trusted_tool_roots(app_dir, tools_dir):
    roots = []
    for value in (app_dir, tools_dir, Path(sys.executable).resolve().parent):
        if value:
            roots.append(Path(value).resolve())
    return tuple(roots)
