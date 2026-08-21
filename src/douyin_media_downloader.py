import argparse
import base64
import ctypes
import json
import logging
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx
from streamget.platforms.douyin.live_stream import DouyinLiveStream

from douyin_abogus import ABogus, BrowserFingerprintGenerator


APP_DIR = Path(__file__).resolve().parent
PACK_ROOT = APP_DIR.parent.parent
ROOT_DOWNLOAD_DIR = APP_DIR.parent
TOOLS_DIR = PACK_ROOT / "youtube-dl"
PROFILES_FILE = APP_DIR / "profiles.json"
SETTINGS_FILE = APP_DIR / "settings.json"
SESSION_FILE = APP_DIR / "douyin_session.json"
MEDIA_BROWSER_HEALTH_FILE = APP_DIR / "media_browser_health.json"
DEFAULT_CHROME_CDP = "http://127.0.0.1:9222"
# Shared Edge profile used for login and Douyin's browser-signed public-video
# requests so the fetch browser can reuse the authenticated session.
FETCH_BROWSER_PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA") or APP_DIR) / "DouyinLiveRecorder" / "PublicEdgeFetchBrowser"
FETCH_BROWSER_CDP_PORT = 9344
FETCH_BROWSER_CDP = f"http://127.0.0.1:{FETCH_BROWSER_CDP_PORT}"
KNOWN_GOOD_EDGE_VERSION = "150.0.4078.83"
MEDIA_BROWSER_LAUNCH_LOCK = threading.Lock()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
POST_PATH = "/aweme/v1/web/aweme/post/"
FAMILIAR_FEED_PATH = "/aweme/v1/web/familiar/feed/"
LIFE_FEED_PATH = "/aweme/v1/life/feed/"
STORY_FEED_PATH = "/aweme/v1/story/feed/"
STORY_PROFILE_LIST_PATH = "/aweme/v1/story/profile/list/"
NEW_STORY_FEED_PATH = "/aweme/v1/new/story/feed/"
NEW_STORY_FEED_V2_PATH = "/aweme/v2/new/story/feed/"
LIFE_FEED_HOSTS = (
    "https://aweme.snssdk.com",
    "https://api5-normal-c-lf.amemv.com",
    "https://api3-normal-c.amemv.com",
)
STORY_PATH_CANDIDATES = (
    FAMILIAR_FEED_PATH,
    "/aweme/v1/web/story/list/",
    "/aweme/v1/web/aweme/moment/list/",
    "/aweme/v1/web/moment/list/",
)
STORY_NESTED_LIST_KEYS = (
    "story_list",
    "all_story_list",
    "stories",
    "aweme_list",
    "story",
    "item_list",
)
MOBILE_ONLY_STORY_MESSAGE = (
    "No downloadable 24-hour story media is visible to this login."
)
_SESSION_REQUIRED_COOKIES = {"sessionid", "sessionid_ss", "uid_tt", "uid_tt_ss"}

# ---------------------------------------------------------------------------
# Mobile API story fetching via X-Gorgon signing.
# aweme_type=68 is Douyin's image-note (图文) type, not a 24h/日常 story.
# Only keep type-68 posts that also carry time-limited story markers.
# Active 24h rings live on /aweme/v1/story/feed/ and /aweme/v1/life/feed/.
# ---------------------------------------------------------------------------
MOBILE_API_HOST = "https://aweme.snssdk.com"
MOBILE_API_AID = 1128
MOBILE_API_UA = (
    "com.ss.android.ugc.aweme/380700 (Linux; U; Android 15; zh_CN; "
    "SM-A5560; Build/AP3A.240905.015.A2)"
)
MOBILE_STORY_AWEME_TYPE = 68
MOBILE_DEVICE_FILE = APP_DIR / "mobile_device.json"
MOBILE_SESSION_FILE = APP_DIR / "mobile_session.json"
_mobile_signer_available = None  # lazy-checked


def _persistent_mobile_device():
    """Reuse one device/install pair so Douyin can attach the app login to it."""
    stored = load_json(MOBILE_DEVICE_FILE, {}) or {}
    device_id = str(stored.get("device_id") or "")
    install_id = str(stored.get("install_id") or "")
    changed = False
    if not (device_id.isdigit() and install_id.isdigit() and len(device_id) >= 16):
        device_id = str(random.randint(7200000000000000000, 7399999999999999999))
        install_id = str(random.randint(7200000000000000000, 7399999999999999999))
        stored["device_id"] = device_id
        stored["install_id"] = install_id
        changed = True
    if not str(stored.get("cdid") or "").strip():
        stored["cdid"] = str(__import__("uuid").uuid4())
        changed = True
    if changed:
        save_json(MOBILE_DEVICE_FILE, stored)
    return device_id, install_id


def _mobile_device_profile():
    """Device/app identity for signed mobile requests. Prefer the bound device pair."""
    stored = load_json(MOBILE_DEVICE_FILE, {}) or {}
    version_code = str(stored.get("version_code") or "380700")
    return {
        "version_code": version_code,
        "version_name": str(stored.get("version_name") or "38.7.0"),
        "device_type": str(stored.get("device_type") or "SM-A5560"),
        "device_brand": str(stored.get("device_brand") or "samsung"),
        "os_version": str(stored.get("os_version") or "15"),
        "os_api": str(stored.get("os_api") or "35"),
        "own_uid": str(stored.get("own_uid") or ""),
        "cdid": str(stored.get("cdid") or ""),
        "channel": str(stored.get("channel") or "channel_aweme"),
        "update_version_code": str(stored.get("update_version_code") or version_code),
    }


def _check_mobile_signer():
    """Check if the X-Gorgon signer module is available."""
    global _mobile_signer_available
    if _mobile_signer_available is not None:
        return _mobile_signer_available
    try:
        from signer.gorgon import get_xgorgon  # noqa: F401
        _mobile_signer_available = True
    except Exception:
        _mobile_signer_available = False
    return _mobile_signer_available


def _mobile_base_params(device_id, install_id):
    """Build common mobile API query parameters."""
    import hashlib
    ident = _mobile_device_profile()
    ts = int(time.time())
    cdid = str(ident.get("cdid") or "").strip()
    if not cdid:
        cdid = hashlib.md5(f"{device_id}:{install_id}".encode()).hexdigest()
        cdid = f"{cdid[:8]}-{cdid[8:12]}-{cdid[12:16]}-{cdid[16:20]}-{cdid[20:32]}"
    return {
        "os": "android",
        "os_api": ident.get("os_api") or "35",
        "os_version": ident.get("os_version") or "15",
        "device_platform": "android",
        "device_type": ident.get("device_type") or "SM-A5560",
        "device_brand": ident.get("device_brand") or "samsung",
        "host_abi": "arm64-v8a",
        "resolution": "1080*2400",
        "dpi": "420",
        "language": "zh",
        "region": "CN",
        "sys_region": "CN",
        "locale": "zh_CN",
        "mcc_mnc": "46000",
        "carrier_region": "CN",
        "timezone_name": "Asia/Shanghai",
        "timezone_offset": "28800",
        "ac": "wifi",
        "channel": ident.get("channel") or "channel_aweme",
        "aid": str(MOBILE_API_AID),
        "app_name": "aweme",
        "version_code": ident.get("version_code") or "380700",
        "version_name": ident.get("version_name") or "38.7.0",
        "update_version_code": ident.get("update_version_code") or ident.get("version_code") or "380700",
        "device_id": device_id,
        "iid": install_id,
        "install_id": install_id,
        "openudid": hashlib.md5(device_id.encode()).hexdigest()[:16],
        "cdid": cdid,
        "ssmix": "a",
        "ts": str(ts),
        "_rticket": str(int(time.time() * 1000)),
        "app_type": "normal",
    }


def _mobile_signed_get(client, path, extra_params, cookie_header,
                       device_id, install_id, *, full_sign=False):
    """Make a signed GET request to the Douyin mobile API."""
    from signer.gorgon import get_xgorgon
    params = _mobile_base_params(device_id, install_id)
    params.update(extra_params)
    query_string = urllib.parse.urlencode(params)
    ts = time.time()
    khronos = int(ts)
    gorgon = get_xgorgon(
        params=query_string, ticket=ts, data="", cookie=cookie_header,
    )
    headers = {
        "User-Agent": MOBILE_API_UA,
        "Cookie": cookie_header,
        "X-Gorgon": gorgon,
        "X-Khronos": str(khronos),
    }
    if full_sign:
        try:
            from signer.argus import Argus
            from signer.ladon import Ladon
            headers["X-Argus"] = Argus.get_sign(
                queryhash=query_string,
                data=None,
                timestamp=khronos,
                aid=MOBILE_API_AID,
            )
            headers["X-Ladon"] = Ladon.encrypt(khronos, "1611921764", MOBILE_API_AID)
        except Exception:
            logging.debug("Argus/Ladon signing unavailable for %s", path, exc_info=True)
    url = f"{MOBILE_API_HOST}{path}?{query_string}"
    return client.get(url, headers=headers)


def _mobile_signed_request(client, method, path, extra_params, cookie_header,
                           device_id, install_id, *, body="", host=None, full_sign=True):
    """Signed mobile GET/POST. POST bodies are included in Gorgon/Argus hashes."""
    from signer.gorgon import get_xgorgon
    params = _mobile_base_params(device_id, install_id)
    params.update(extra_params or {})
    query_string = urllib.parse.urlencode(params)
    ts = time.time()
    khronos = int(ts)
    gorgon = get_xgorgon(
        params=query_string, ticket=ts, data=body or "", cookie=cookie_header,
    )
    headers = {
        "User-Agent": MOBILE_API_UA,
        "Cookie": cookie_header,
        "X-Gorgon": gorgon,
        "X-Khronos": str(khronos),
    }
    if body:
        from hashlib import md5 as _md5
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-SS-STUB"] = _md5(body.encode("utf-8")).hexdigest().upper()
        headers["X-SS-REQ-TICKET"] = str(int(ts * 1000))
    if full_sign:
        try:
            from hashlib import md5 as _md5
            from signer.argus import Argus
            from signer.ladon import Ladon
            stub = _md5((body or "").encode("utf-8")).hexdigest() if body else None
            headers["X-Argus"] = Argus.get_sign(
                queryhash=query_string,
                data=stub,
                timestamp=khronos,
                aid=MOBILE_API_AID,
            )
            headers["X-Ladon"] = Ladon.encrypt(khronos, "1611921764", MOBILE_API_AID)
        except Exception:
            logging.debug("Argus/Ladon signing unavailable for %s %s", method, path, exc_info=True)
    url = f"{(host or MOBILE_API_HOST)}{path}?{query_string}"
    if str(method).upper() == "POST":
        return client.post(url, headers=headers, content=(body or "").encode("utf-8"))
    return client.get(url, headers=headers)


def _is_mobile_post_story(aweme):
    """True when a post-feed item is itself a time-limited 日常, not just 图文."""
    if not isinstance(aweme, dict):
        return False
    if aweme.get("aweme_type") == MOBILE_STORY_AWEME_TYPE and is_time_limited_story(aweme):
        return True
    return is_time_limited_story(aweme)


def fetch_stories_via_mobile_post_api(client, sec_user_id, cookie_header=""):
    """
    Fetch time-limited story items from the mobile /aweme/v1/aweme/post/ feed.

    aweme_type=68 alone is not enough: that is the regular image-note type and
    those posts stay in the profile feed forever. Only items that also carry
    24h/日常 markers are treated as stories.

    Returns (items_list, source_label) or (None, error_message).
    """
    if not _check_mobile_signer():
        return None, "mobile_post_api: signer module not available"
    if not cookie_header:
        cookie_header = _mobile_cookie_header()
    if not cookie_header:
        return None, "mobile_post_api: no session cookies"

    device_id, install_id = _persistent_mobile_device()

    stories = []
    max_cursor = "0"
    pages_fetched = 0
    max_pages = 15  # safety limit

    try:
        while pages_fetched < max_pages:
            response = _mobile_signed_get(
                client,
                "/aweme/v1/aweme/post/",
                {
                    "sec_user_id": sec_user_id,
                    "count": "20",
                    "max_cursor": max_cursor,
                },
                cookie_header,
                device_id,
                install_id,
            )
            data = response.json()
            if not isinstance(data, dict):
                break
            status_code = data.get("status_code")
            if status_code != 0:
                msg = data.get("status_msg") or data.get("message") or ""
                if pages_fetched == 0:
                    return None, f"mobile_post_api: status_code={status_code} {msg}"
                break  # partial results are still useful

            aweme_list = data.get("aweme_list") or []
            for aweme in aweme_list:
                if _is_mobile_post_story(aweme):
                    stories.append(aweme)

            has_more = data.get("has_more", 0)
            max_cursor = str(data.get("max_cursor", 0))
            pages_fetched += 1

            if not has_more:
                break
            time.sleep(0.3)  # rate-limit courtesy

    except Exception as exc:
        if not stories:
            return None, f"mobile_post_api: {exc}"
        # Return partial results

    if stories:
        return stories, f"{MOBILE_API_HOST}/aweme/v1/aweme/post/ (mobile, {len(stories)} stories)"
    return None, "mobile_post_api: no time-limited stories in post feed"


def _story_items_from_feed_payload(data):
    """Extract aweme dicts from /aweme/v1/story/feed/ or similar packs."""
    if not isinstance(data, dict):
        return []
    items = []
    raw = data.get("data")
    active = data.get("active_data")
    if raw in (None, [], {}) and isinstance(active, dict):
        raw = active.get("data") if active.get("data") not in (None, [], {}) else active
    entries = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = [raw]
        for key in ("aweme_list", "story_list", "all_story_list", "user_story_list", "items", "data"):
            nested = raw.get(key)
            if isinstance(nested, list):
                entries.extend(nested)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        packed = _story_items_from_user_entry(entry)
        if packed:
            items.extend(packed)
            continue
        aweme = entry.get("aweme") or entry.get("aweme_info")
        if isinstance(aweme, dict) and (aweme.get("aweme_id") or aweme.get("group_id")):
            items.append(aweme)
        elif entry.get("aweme_id") or entry.get("group_id"):
            items.append(entry)
    if not items:
        items = normalize_items(data)
    seen = set()
    unique = []
    for item in items:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("aweme_id") or item.get("group_id") or "")
        if aweme_id and aweme_id in seen:
            continue
        if aweme_id:
            seen.add(aweme_id)
        unique.append(item)
    return unique


def fetch_stories_via_mobile_story_feed(client, sec_user_id, user_id="", cookie_header=""):
    """
    Fetch the active 24h/日常 pack via mobile story endpoints.

    The follow-tab tray is /aweme/v1/story/feed/. Story25 (日常) also lives on
    /aweme/v1/new/story/feed/, /aweme/v2/new/story/feed/, and the profile
    tab /aweme/v1/story/profile/list/. Returns (items, source) or (None, msg).
    """
    if not _check_mobile_signer():
        return None, "mobile_story_feed: signer module not available"
    if not cookie_header:
        cookie_header = _mobile_cookie_header()
    if not cookie_header:
        return None, "mobile_story_feed: no session cookies"

    numeric_uid = str(user_id or "").strip()
    if not numeric_uid.isdigit():
        numeric_uid = resolve_numeric_user_id(client, {"cookies": cookie_header}, sec_user_id)
    device_id, install_id = _persistent_mobile_device()
    own_uid = _mobile_device_profile().get("own_uid") or ""

    path_variants = []
    if numeric_uid.isdigit():
        # Captured from the app's Story25Api.getFeeds (profile 日常 tab).
        profile_extra = {
            "to_uid": numeric_uid,
            "offset": "0",
            "story_ttl": "7",
            "active_data_size": "20",
            "month_data_size": "4",
            "insert_ids": "",
            "delete_ids": "",
        }
        path_variants.append((STORY_PROFILE_LIST_PATH, profile_extra))

    insert_json = json.dumps([int(numeric_uid)]) if numeric_uid.isdigit() else ""
    viewer_uid = own_uid if own_uid.isdigit() else numeric_uid
    tray_extra = {"cursor": "0", "count": "20", "source": "1", "sec_user_id": sec_user_id}
    if viewer_uid.isdigit():
        tray_extra["user_id"] = viewer_uid
    if insert_json:
        tray_extra["insert_ids"] = insert_json
        tray_extra["filter_warn"] = "0"
        tray_extra["user_per_page"] = "0"
    for path in (STORY_FEED_PATH, NEW_STORY_FEED_PATH, NEW_STORY_FEED_V2_PATH):
        path_variants.append((path, dict(tray_extra)))

    last_message = "mobile_story_feed: no items"
    hosts = (MOBILE_API_HOST, "https://api3-social-m-lf.amemv.com")
    for path, extra in path_variants:
        try_hosts = hosts if path == STORY_PROFILE_LIST_PATH else (MOBILE_API_HOST,)
        for host in try_hosts:
            try:
                response = _mobile_signed_request(
                    client,
                    "GET",
                    path,
                    extra,
                    cookie_header,
                    device_id,
                    install_id,
                    host=host,
                    full_sign=True,
                )
                data = response.json()
            except Exception as exc:
                last_message = f"mobile_story_feed: {host}{path}: {exc}"
                continue
            if not isinstance(data, dict):
                continue
            status_code = data.get("status_code")
            if status_code not in (0, None):
                last_message = (
                    f"mobile_story_feed: {path}: status_code={status_code} "
                    f"{(data.get('status_msg') or data.get('message') or '')}"
                )
                continue
            items = _story_items_from_feed_payload(data)
            if not items:
                items = normalize_items(data)
            if items:
                return items, f"{host}{path}"
            last_message = f"{host}{path}: empty pack"
            # profile/list is the real 日常 tab. status 0 + no items means
            # there is no active story, even if web story_tab_empty is false.
            if path == STORY_PROFILE_LIST_PATH:
                return None, last_message
    return None, last_message


def fetch_stories_via_mobile_life_feed(client, sec_user_id, user_id="", cookie_header=""):
    """
    Fetch the active 24h/日常 pack via a signed mobile POST to /aweme/v1/life/feed/.

    The web a_bogus POST often returns an accepted empty pack. The app endpoint
    expects Gorgon + Argus over the form body. Returns (items, source) or
    (None, message).
    """
    if not _check_mobile_signer():
        return None, "mobile_life_feed: signer module not available"
    if not cookie_header:
        cookie_header = _mobile_cookie_header()
    if not cookie_header:
        return None, "mobile_life_feed: no session cookies"

    numeric_uid = str(user_id or "").strip()
    if not numeric_uid.isdigit():
        numeric_uid = resolve_numeric_user_id(client, {"cookies": cookie_header}, sec_user_id)
    if not numeric_uid.isdigit():
        return None, "mobile_life_feed: could not resolve numeric user_id"

    device_id, install_id = _persistent_mobile_device()
    bodies = (
        urllib.parse.urlencode(
            {
                "user_ids": json.dumps([int(numeric_uid)]),
                "sec_user_ids": json.dumps([sec_user_id]),
                "count": "20",
                "cursor": "0",
                "pull_type": "2",
            }
        ),
        urllib.parse.urlencode(
            {
                "user_ids": json.dumps([int(numeric_uid)]),
                "count": "20",
                "cursor": "0",
                "pull_type": "2",
            }
        ),
        urllib.parse.urlencode(
            {
                "user_ids": numeric_uid,
                "count": "20",
                "cursor": "0",
            }
        ),
    )
    last_message = "mobile_life_feed: no items"
    last_ok = None
    for host in LIFE_FEED_HOSTS:
        for body in bodies:
            try:
                response = _mobile_signed_request(
                    client,
                    "POST",
                    LIFE_FEED_PATH,
                    {},
                    cookie_header,
                    device_id,
                    install_id,
                    body=body,
                    host=host,
                    full_sign=True,
                )
                data = response.json()
            except Exception as exc:
                last_message = f"{host}{LIFE_FEED_PATH}: {exc}"
                continue
            if not isinstance(data, dict):
                continue
            try:
                status_code = int(data.get("status_code") or 0)
            except (TypeError, ValueError):
                status_code = -1
            if status_code != 0:
                last_message = (
                    f"{host}{LIFE_FEED_PATH}: status_code={status_code} "
                    f"{(data.get('status_msg') or data.get('message') or '')}"
                )
                continue
            last_ok = data
            items = normalize_items(data)
            if not items:
                items = _story_items_from_feed_payload(data)
            if items:
                return items, f"{host}{LIFE_FEED_PATH}"
            last_message = f"{host}{LIFE_FEED_PATH}: empty pack"
    if last_ok is not None:
        return None, last_message
    return None, last_message


def fetch_posts_via_mobile_api(client, sec_user_id, limit=0, cookie_header=""):
    """
    Fetch ALL post items (videos + images + stories) via the mobile
    /aweme/v1/aweme/post/ endpoint with X-Gorgon signing.

    This is the app-login post list. The bound device_id/install_id must stay
    stable so Douyin keeps treating the EXE QR session as that app.

    Returns list of aweme dicts, or raises on total failure.
    """
    if not _check_mobile_signer():
        raise EmptyApiResponseError("mobile_post_api: signer module not available")
    if not cookie_header:
        cookie_header = _mobile_cookie_header()
    if not cookie_header:
        raise EmptyApiResponseError("mobile_post_api: no session cookies")

    device_id, install_id = _persistent_mobile_device()

    all_items = []
    max_cursor = "0"
    pages_fetched = 0
    max_pages = 30

    while pages_fetched < max_pages:
        response = _mobile_signed_get(
            client,
            "/aweme/v1/aweme/post/",
            {"sec_user_id": sec_user_id, "count": "20", "max_cursor": max_cursor},
            cookie_header,
            device_id,
            install_id,
        )
        data = response.json()
        if not isinstance(data, dict) or data.get("status_code") != 0:
            if pages_fetched == 0:
                msg = (data.get("status_msg") or data.get("message") or "") if isinstance(data, dict) else ""
                raise EmptyApiResponseError(f"mobile_post_api: {msg}")
            break

        aweme_list = data.get("aweme_list") or []
        all_items.extend(aweme_list)
        pages_fetched += 1

        if limit and len(all_items) >= limit:
            return all_items[:limit]
        if not data.get("has_more", 0):
            break
        max_cursor = str(data.get("max_cursor", 0))
        time.sleep(0.3)

    if not all_items:
        raise EmptyApiResponseError("mobile_post_api: no posts returned")
    return all_items





class LoginRequiredError(RuntimeError):
    pass


class EmptyApiResponseError(RuntimeError):
    """Douyin returned a successful HTTP response without an API payload."""

    pass


class CaptchaDetectedError(RuntimeError):
    """Douyin is showing a captcha / slide-verify challenge."""

    pass


# OPT-D: Track consecutive HTTP fast-path failures per sec_user_id.
# After 3 consecutive empty/failed HTTP attempts, skip the HTTP path
# and go straight to browser to avoid wasting ~2-3s per cycle.
# FIX-4.1: Store (count, last_failure_timestamp) so the counter auto-resets
# after a cooldown period, preventing permanent fast-path disablement from
# transient network issues.
_http_fastpath_failures: dict = {}  # sec_user_id -> (failure_count, last_failure_time)
_http_fastpath_lock = threading.Lock()  # FIX-7.1: protect non-atomic read-modify-write
_HTTP_FASTPATH_MAX_FAILURES = 3
_HTTP_FASTPATH_COOLDOWN = 600  # seconds (10 min) before retrying HTTP path


def report_progress(callback, **progress):
    if not callback:
        return
    try:
        callback(progress)
    except Exception:
        # UI progress must never be able to interrupt a media download.
        pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob_from_bytes(data):
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def dpapi_protect(data):
    if os.name != "nt":
        raise RuntimeError("Douyin login storage currently requires Windows")
    input_blob, input_buffer = _blob_from_bytes(data)
    output_blob = _DataBlob()
    description = "Douyin Live Recorder session"
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob), description, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def dpapi_unprotect(data):
    if os.name != "nt":
        raise RuntimeError("Douyin login storage currently requires Windows")
    input_blob, input_buffer = _blob_from_bytes(data)
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def save_session_cookie_header(cookie_header, source=DEFAULT_CHROME_CDP):
    encrypted = dpapi_protect(cookie_header.encode("utf-8"))
    save_json(
        SESSION_FILE,
        {
            "format": "windows-dpapi-v1",
            "encrypted_cookie_header": base64.b64encode(encrypted).decode("ascii"),
            "source": source,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def load_session_cookie_header():
    session = load_json(SESSION_FILE, {})
    encoded = session.get("encrypted_cookie_header") if isinstance(session, dict) else ""
    if not encoded:
        return ""
    try:
        encrypted = base64.b64decode(encoded)
        return dpapi_unprotect(encrypted).decode("utf-8")
    except Exception:
        logging.warning("Saved Douyin session could not be decrypted; treating as logged out.")
        return ""


def save_mobile_session_cookie_header(cookie_header, source="edge-qr-app"):
    """Persist the EXE QR login as the app-capable mobile session."""
    encrypted = dpapi_protect(cookie_header.encode("utf-8"))
    save_json(
        MOBILE_SESSION_FILE,
        {
            "format": "windows-dpapi-v1",
            "encrypted_cookie_header": base64.b64encode(encrypted).decode("ascii"),
            "source": source,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def load_mobile_session_cookie_header():
    session = load_json(MOBILE_SESSION_FILE, {})
    encoded = session.get("encrypted_cookie_header") if isinstance(session, dict) else ""
    if not encoded:
        return ""
    try:
        encrypted = base64.b64decode(encoded)
        return dpapi_unprotect(encrypted).decode("utf-8")
    except Exception:
        logging.warning("Saved mobile session could not be decrypted; treating as logged out.")
        return ""


def _session_cookie_names(cookie_header):
    return {
        part.split("=", 1)[0].strip()
        for part in (cookie_header or "").split(";")
        if "=" in part and part.split("=", 1)[0].strip()
    }


def _session_is_logged_in(cookie_header):
    names = _session_cookie_names(cookie_header)
    return bool((cookie_header or "").strip() and names.intersection(_SESSION_REQUIRED_COOKIES))


def promote_web_session_to_app():
    """One EXE QR login is enough: copy web cookies into the app session and bind a device."""
    header = load_mobile_session_cookie_header() or load_session_cookie_header()
    if not header:
        return ""
    if not load_mobile_session_cookie_header():
        save_mobile_session_cookie_header(header, source="edge-qr-app")
    _persistent_mobile_device()
    return header


def _mobile_cookie_header(fallback=""):
    """Unified EXE login cookies. Web QR cookies work as the app session with the bound device."""
    return (
        load_mobile_session_cookie_header()
        or (fallback or "").strip()
        or load_session_cookie_header()
    )


def _read_http_headers(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("Chrome closed the debugging connection during login import")
        data.extend(chunk)
        if len(data) > 1024 * 1024:
            raise RuntimeError("Unexpected Chrome debugging response")
    marker = data.index(b"\r\n\r\n") + 4
    return bytes(data[:marker]), bytes(data[marker:])


def _masked_websocket_text(payload):
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    length = len(data)
    if length < 126:
        header = bytes((0x81, 0x80 | length))
    elif length < 65536:
        header = bytes((0x81, 0x80 | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((0x81, 0x80 | 127)) + length.to_bytes(8, "big")
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    return header + mask + masked


def _extract_websocket_frames(buffer):
    frames = []
    offset = 0
    while len(buffer) - offset >= 2:
        first, second = buffer[offset], buffer[offset + 1]
        payload_length = second & 0x7F
        header_length = 2
        if payload_length == 126:
            if len(buffer) - offset < 4:
                break
            payload_length = int.from_bytes(buffer[offset + 2 : offset + 4], "big")
            header_length = 4
        elif payload_length == 127:
            if len(buffer) - offset < 10:
                break
            payload_length = int.from_bytes(buffer[offset + 2 : offset + 10], "big")
            header_length = 10
        masked = bool(second & 0x80)
        mask_length = 4 if masked else 0
        frame_length = header_length + mask_length + payload_length
        if len(buffer) - offset < frame_length:
            break
        payload_start = offset + header_length
        mask = buffer[payload_start : payload_start + 4] if masked else b""
        payload_start += mask_length
        payload = bytearray(buffer[payload_start : payload_start + payload_length])
        if masked:
            for index in range(len(payload)):
                payload[index] ^= mask[index % 4]
        frames.append((first & 0x0F, bool(first & 0x80), bytes(payload)))
        offset += frame_length
    return frames, buffer[offset:]


def chrome_cdp_command(websocket_url, method, params=None, timeout=15):
    with CdpSession(websocket_url, timeout=timeout) as session:
        return session.call(method, params or {}, timeout=timeout)


class CdpSession:
    """Long-lived CDP WebSocket that can both send commands and read events."""

    def __init__(self, websocket_url, timeout=30):
        self.websocket_url = websocket_url
        self.timeout = timeout
        self._sock = None
        self._buffered = b""
        self._next_id = random.randint(1000, 999999)
        self._pending = {}
        self._events = []

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def connect(self):
        if self._sock is not None:
            return
        parsed = urllib.parse.urlparse(self.websocket_url)
        host = (parsed.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(f"Refusing CDP WebSocket host that is not loopback: {host or '?'}")
        sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("ascii"))
        response_head, buffered = _read_http_headers(sock)
        if not response_head.startswith(b"HTTP/1.1 101"):
            sock.close()
            raise RuntimeError("Chrome rejected the debugging connection")
        self._sock = sock
        self._buffered = buffered

    def close(self):
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _recv_message(self, timeout=None):
        if self._sock is None:
            raise RuntimeError("CDP session is not connected")
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        fragments = bytearray()
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Chrome debugging message")
            self._sock.settimeout(max(0.1, remaining))
            frames, self._buffered = _extract_websocket_frames(self._buffered)
            for opcode, final, payload in frames:
                if opcode == 0x8:
                    raise RuntimeError("Chrome closed the debugging connection")
                if opcode == 0x1:
                    fragments = bytearray(payload)
                elif opcode == 0x0:
                    fragments.extend(payload)
                else:
                    continue
                if not final:
                    continue
                return json.loads(fragments.decode("utf-8"))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise TimeoutError("Timed out waiting for Chrome debugging message") from exc
            if not chunk:
                raise RuntimeError("Chrome closed the debugging connection")
            self._buffered += chunk

    def call(self, method, params=None, timeout=None):
        if self._sock is None:
            self.connect()
        request_id = self._next_id
        self._next_id += 1
        command = json.dumps({"id": request_id, "method": method, "params": params or {}})
        self._sock.sendall(_masked_websocket_text(command))
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for CDP result: {method}")
            message = self._recv_message(timeout=remaining)
            if message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(message["error"].get("message") or f"CDP error: {method}")
                return message.get("result") or {}
            if message.get("method"):
                self._events.append(message)

    def drain_events(self):
        events = self._events
        self._events = []
        # Also non-blocking poll for any already-buffered frames.
        if self._sock is None:
            return events
        try:
            self._sock.settimeout(0.01)
            while True:
                try:
                    message = self._recv_message(timeout=0.01)
                except TimeoutError:
                    break
                if message.get("method"):
                    events.append(message)
                elif message.get("id") is not None:
                    # Unexpected late response; keep as event-like payload.
                    events.append(message)
        except Exception:
            pass
        finally:
            try:
                self._sock.settimeout(self.timeout)
            except Exception:
                pass
        return events

    def wait_for_events(self, predicate, timeout=20, poll=0.15):
        """Read events until predicate(events_so_far) is true or timeout."""
        collected = list(self.drain_events())
        if predicate(collected):
            return collected
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                message = self._recv_message(timeout=min(poll, max(0.05, remaining)))
            except TimeoutError:
                if predicate(collected):
                    return collected
                continue
            if message.get("method"):
                collected.append(message)
                if predicate(collected):
                    return collected
            elif message.get("id") is not None and message.get("error"):
                raise RuntimeError(message["error"].get("message") or "CDP error")
        if predicate(collected):
            return collected
        return collected


def _parse_cookie_header_pairs(cookie_header):
    pairs = []
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        pairs.append((name, value.strip()))
    return pairs


def _cdp_list_targets(cdp_url):
    endpoint = cdp_url.rstrip("/") + "/json/list"
    with httpx.Client(trust_env=False) as client:
        response = client.get(endpoint, timeout=5)
    response.raise_for_status()
    targets = response.json()
    return targets if isinstance(targets, list) else []


def _cdp_browser_info(cdp_url):
    endpoint = cdp_url.rstrip("/") + "/json/version"
    with httpx.Client(trust_env=False) as client:
        response = client.get(endpoint, timeout=5)
    response.raise_for_status()
    data = response.json()
    browser = str(data.get("Browser") or "")
    product, _, version = browser.partition("/")
    return {"browser": browser, "product": product, "version": version}


def _require_edge_browser(cdp_url):
    info = _cdp_browser_info(cdp_url)
    if info["product"] != "Edg":
        raise RuntimeError(
            f"Public-video capture requires Microsoft Edge; port {cdp_url} is {info['browser'] or 'an unknown browser'}"
        )
    return info


def _record_media_browser_success(cdp_url):
    info = _require_edge_browser(cdp_url)
    previous = load_json(MEDIA_BROWSER_HEALTH_FILE, {})
    previous_version = str(previous.get("last_good_version") or "") if isinstance(previous, dict) else ""
    if previous_version and previous_version != info["version"]:
        logging.info(
            "Douyin media browser changed from last-good Edge %s to %s and passed a real video-list scan.",
            previous_version,
            info["version"],
        )
    save_json(
        MEDIA_BROWSER_HEALTH_FILE,
        {
            "browser": info["browser"],
            "last_good_version": info["version"],
            "known_good_at_fix": KNOWN_GOOD_EDGE_VERSION,
            "last_success_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return info


def _cdp_pick_page(cdp_url, prefer_douyin=True):
    targets = _cdp_list_targets(cdp_url)
    pages = [
        item
        for item in targets
        if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
    ]
    if not pages:
        return None
    if prefer_douyin:
        for item in pages:
            url = str(item.get("url") or "")
            if url.startswith("https://www.douyin.com/"):
                return item
    return pages[0]


def _ensure_media_fetch_browser(port=FETCH_BROWSER_CDP_PORT):
    """Start (or reuse) the dedicated anonymous video-list browser."""
    cdp_url = f"http://127.0.0.1:{port}"
    if cdp_is_available(cdp_url):
        info = _require_edge_browser(cdp_url)
        return {"cdp_url": cdp_url, "reused": True, **info}
    browser_path = find_media_browser_executable()
    FETCH_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser_path),
        "--headless=new",
        "--disable-gpu",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={FETCH_BROWSER_PROFILE_DIR}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--disable-background-networking",
        "--disable-sync",
        "--window-size=1280,900",
        "about:blank",  # OPT-H: skip initial douyin.com load; media check navigates directly
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        # FIX-3.1: Detect early browser crash (e.g. port already in use).
        if process.poll() is not None:
            raise RuntimeError(
                f"Media fetch browser exited immediately with code {process.returncode}. "
                f"Port {port} may be occupied by a stale process."
            )
        if cdp_is_available(cdp_url):
            info = _require_edge_browser(cdp_url)
            logging.info("Douyin public-video browser ready: %s", info["browser"])
            return {
                "cdp_url": cdp_url,
                "reused": False,
                "process": process,
                "browser_path": str(browser_path),
                "profile_dir": str(FETCH_BROWSER_PROFILE_DIR),
                **info,
            }
        time.sleep(0.25)
    # FIX-6.1: Kill the orphaned process to prevent it from holding the
    # user-data-dir lock and blocking future browser launches.
    try:
        process.kill()
        process.wait(timeout=5)
    except Exception:
        pass
    raise RuntimeError(
        f"Media fetch browser started but debugging port {port} never became ready "
        f"(process killed to release profile lock)"
    )


def ensure_media_fetch_browser(port=FETCH_BROWSER_CDP_PORT):
    with MEDIA_BROWSER_LAUNCH_LOCK:
        return _ensure_media_fetch_browser(port)



# FIX-CAPTCHA-2: Track consecutive captcha resets to avoid vicious cycle.
# Wiping cookies on EVERY captcha detection creates a loop:
#   detect captcha -> wipe cookies -> fresh browser -> Douyin flags new browser -> captcha -> repeat
# Only wipe cookies after 3 consecutive resets within 30 minutes.
_captcha_reset_count = 0
_captcha_reset_first_ts = 0.0
_CAPTCHA_RESET_WIPE_THRESHOLD = 3
_CAPTCHA_RESET_WINDOW = 1800  # 30 min window for counting consecutive resets


def reset_media_fetch_browser(port=FETCH_BROWSER_CDP_PORT):
    """Kill the media fetch browser so the next profile gets a clean session.

    Called after a confirmed captcha so that flagged cookies / session state
    do not poison subsequent profile checks in the same cycle.
    FIX-CAPTCHA-2: Only wipes cookies after multiple consecutive captcha hits
    to avoid the vicious cycle of wipe -> fresh browser -> flagged -> captcha.
    FIX-6.2: Protected by MEDIA_BROWSER_LAUNCH_LOCK to prevent races with
    concurrent browser launches.
    """
    with MEDIA_BROWSER_LAUNCH_LOCK:  # FIX-6.2: prevent race with launch_media_fetch_browser
        return _reset_media_fetch_browser_inner(port)


def _reset_media_fetch_browser_inner(port):
    global _captcha_reset_count, _captcha_reset_first_ts
    cdp_url = f"http://127.0.0.1:{port}"
    # Find the browser process via the debugging port.
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
        )
        for line in (result.stdout or "").splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                    logging.info("Killed media fetch browser (PID %d) after captcha.", pid)
                    break
    except Exception:
        logging.debug("Could not kill media fetch browser by port.", exc_info=True)
    # Also try the CDP /json/close endpoint as a graceful fallback.
    try:
        with httpx.Client(trust_env=False) as client:
            targets = client.get(cdp_url.rstrip("/") + "/json/list", timeout=3).json()
            for target in targets:
                tid = target.get("id")
                if tid:
                    client.get(cdp_url.rstrip("/") + f"/json/close/{tid}", timeout=3)
    except Exception:
        pass
    # FIX-CAPTCHA-2b: Only wipe cookies/cache after repeated captcha hits.
    # A single captcha may be transient; wiping immediately creates a vicious
    # cycle where the fresh browser gets flagged again.
    now = time.time()
    if _captcha_reset_first_ts == 0 or (now - _captcha_reset_first_ts) > _CAPTCHA_RESET_WINDOW:
        _captcha_reset_count = 0
        _captcha_reset_first_ts = now
    _captcha_reset_count += 1
    if _captcha_reset_count >= _CAPTCHA_RESET_WIPE_THRESHOLD:
        for cookie_file in ("Cookies", "Cookies-journal"):
            try:
                (FETCH_BROWSER_PROFILE_DIR / "Default" / cookie_file).unlink(missing_ok=True)
            except Exception:
                pass
        for cache_dir in ("Cache", "Code Cache", "GPUCache"):
            try:
                shutil.rmtree(FETCH_BROWSER_PROFILE_DIR / "Default" / cache_dir, ignore_errors=True)
            except Exception:
                pass
        logging.info(
            "Cleared browser cookies and cache after %d consecutive captcha resets.",
            _captcha_reset_count,
        )
        _captcha_reset_count = 0
        _captcha_reset_first_ts = 0.0
    else:
        logging.info(
            "Captcha reset %d/%d - keeping cookies (wipe after %d consecutive).",
            _captcha_reset_count, _CAPTCHA_RESET_WIPE_THRESHOLD, _CAPTCHA_RESET_WIPE_THRESHOLD,
        )
    time.sleep(1)


def _cdp_apply_session_cookies(session, cookie_header):
    """Apply session cookies to the browser via CDP.

    FIX-2.1: Set cookies on multiple relevant domains so that requests to
    iesdouyin.com and other ByteDance subdomains also carry auth tokens.
    """
    pairs = _parse_cookie_header_pairs(cookie_header)
    applied = 0
    # Apply to both .douyin.com and .iesdouyin.com for full coverage.
    domains = (".douyin.com", ".iesdouyin.com")
    for name, value in pairs:
        for domain in domains:
            try:
                session.call(
                    "Network.setCookie",
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    },
                    timeout=5,
                )
                applied += 1
            except Exception:
                continue
    return applied


def _cdp_collect_post_payloads(session, sec_user_id, *, timeout=25):
    """Return list of decoded JSON bodies for aweme/post responses."""

    def is_post_url(url):
        text = str(url or "")
        if "/aweme/v1/web/aweme/post" not in text:
            return False
        # Prefer matching the requested profile when the query carries sec_user_id.
        if sec_user_id and "sec_user_id=" in text:
            return urllib.parse.quote(sec_user_id, safe="") in text or sec_user_id in text
        return True

    request_ids = []
    bodies = []
    seen_ids = set()
    harvested_ids = set()  # FIX-5.2: Track harvested request IDs for O(1) lookup

    def harvest(events):
        for event in events:
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = response.get("url") or ""
                request_id = params.get("requestId")
                if request_id and is_post_url(url) and request_id not in seen_ids:
                    request_ids.append(request_id)
                    seen_ids.add(request_id)
            elif method == "Network.loadingFinished":
                request_id = params.get("requestId")
                if request_id in seen_ids and request_id not in harvested_ids:
                    try:
                        result = session.call(
                            "Network.getResponseBody",
                            {"requestId": request_id},
                            timeout=10,
                        )
                    except Exception:
                        continue
                    raw = result.get("body") or ""
                    if result.get("base64Encoded"):
                        try:
                            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
                        except Exception:
                            continue
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        data = dict(data)
                        data["_request_id"] = request_id
                        bodies.append(data)
                        harvested_ids.add(request_id)

    events = session.wait_for_events(
        lambda collected: any(
            (e.get("method") == "Network.responseReceived")
            and is_post_url(((e.get("params") or {}).get("response") or {}).get("url"))
            for e in collected
        ),
        timeout=timeout,
    )
    harvest(events)
    # Give loadingFinished a moment for each captured response.
    if request_ids:
        more = session.wait_for_events(
            lambda collected: len(bodies) >= len(request_ids),
            timeout=8,
        )
        harvest(more)
        # Explicitly pull any remaining bodies.
        for request_id in request_ids:
            # FIX-AUDIT-7: Use O(1) set lookup instead of O(n) linear scan
            if request_id in harvested_ids:
                continue
            try:
                result = session.call(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                    timeout=10,
                )
            except Exception:
                continue
            raw = result.get("body") or ""
            if result.get("base64Encoded"):
                try:
                    raw = base64.b64decode(raw).decode("utf-8", errors="replace")
                except Exception:
                    continue
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict):
                data = dict(data)
                data["_request_id"] = request_id
                bodies.append(data)
    return bodies


def _is_story_capture_url(url):
    text = str(url or "").lower()
    if not text.startswith("http"):
        return False
    markers = (
        "/story/",
        "/life/",
        "/moment/",
        "story/feed",
        "life/feed",
        "life/item",
        "aweme/detail",
        "multi/aweme/detail",
    )
    return any(marker in text for marker in markers)


def _cdp_collect_json_payloads(session, url_predicate, *, timeout=12):
    """Harvest JSON bodies for network responses whose URL matches predicate."""
    request_ids = []
    bodies = []
    seen_ids = set()
    harvested_ids = set()

    def harvest(events):
        for event in events:
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = response.get("url") or ""
                request_id = params.get("requestId")
                if request_id and url_predicate(url) and request_id not in seen_ids:
                    request_ids.append(request_id)
                    seen_ids.add(request_id)
            elif method == "Network.loadingFinished":
                request_id = params.get("requestId")
                if request_id in seen_ids and request_id not in harvested_ids:
                    try:
                        result = session.call(
                            "Network.getResponseBody",
                            {"requestId": request_id},
                            timeout=8,
                        )
                    except Exception:
                        continue
                    raw = result.get("body") or ""
                    if result.get("base64Encoded"):
                        try:
                            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
                        except Exception:
                            continue
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        data = dict(data)
                        data["_source_url"] = ""
                        bodies.append(data)
                        harvested_ids.add(request_id)

    events = session.wait_for_events(
        lambda collected: any(
            (e.get("method") == "Network.responseReceived")
            and url_predicate(((e.get("params") or {}).get("response") or {}).get("url"))
            for e in collected
        ),
        timeout=timeout,
    )
    harvest(events)
    if request_ids:
        more = session.wait_for_events(
            lambda collected: len(bodies) >= len(request_ids),
            timeout=6,
        )
        harvest(more)
    return bodies


def _cdp_click_profile_story_ring(session):
    """Click the profile avatar / story ring if one is visible."""
    result = session.call(
        "Runtime.evaluate",
        {
            "expression": """
(() => {
  const nodes = [...document.querySelectorAll('img, canvas, [class*="avatar"], [class*="Avatar"]')];
  const scored = [];
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 56 || rect.width > 280 || rect.height < 56 || rect.top > 500) continue;
    const src = String(el.src || el.currentSrc || '');
    const cls = String(el.className || '');
    let score = 0;
    if (src.includes('avatar') || src.includes('aweme-avatar')) score += 3;
    if (/avatar|story|ring|日常/i.test(cls)) score += 2;
    if (rect.left < 480) score += 1;
    if (score <= 0) continue;
    scored.push({el, score, src: src.slice(0, 80)});
  }
  scored.sort((a, b) => b.score - a.score);
  const hit = scored[0];
  if (!hit) return {ok: false, reason: 'no-avatar'};
  const target = hit.el.closest('a,button,[role="button"]') || hit.el.parentElement || hit.el;
  target.click();
  return {ok: true, src: hit.src, score: hit.score};
})()
""",
            "returnByValue": True,
        },
        timeout=8,
    )
    return (result.get("result") or {}).get("value") or {}


def fetch_stories_via_browser(profile, sec_user_id, cdp_url=None):
    """
    Open the profile in the logged-in fetch browser, click the story ring,
    and harvest story/life/detail API bodies. Returns (items, source) or
    (None, message). Never wipes the shared fetch-browser session.
    """
    launched = None
    if not cdp_url:
        launched = ensure_media_fetch_browser()
        cdp_url = launched["cdp_url"]
    elif not cdp_is_available(cdp_url):
        launched = ensure_media_fetch_browser()
        cdp_url = launched["cdp_url"]

    page = _cdp_pick_page(cdp_url, prefer_douyin=False)
    if not page:
        try:
            with httpx.Client(trust_env=False) as client:
                client.put(cdp_url.rstrip("/") + "/json/new?about:blank", timeout=5)
        except Exception:
            pass
        time.sleep(0.4)
        page = _cdp_pick_page(cdp_url, prefer_douyin=False)
    if not page:
        return None, "browser_story: no open page target"

    profile_url = f"https://www.douyin.com/user/{sec_user_id}"
    items = []
    try:
        with CdpSession(page["webSocketDebuggerUrl"], timeout=40) as session:
            session.call("Network.enable", {"maxResourceBufferSize": 20 * 1024 * 1024})
            session.call("Page.enable", {})
            cookie_header = (profile.get("cookies") or "").strip() or _mobile_cookie_header()
            if cookie_header:
                _cdp_apply_session_cookies(session, cookie_header)
            session.call("Page.navigate", {"url": profile_url}, timeout=30)
            time.sleep(2)
            if _detect_browser_captcha(session):
                return None, "browser_story: captcha"
            click = _cdp_click_profile_story_ring(session)
            payloads = _cdp_collect_json_payloads(
                session, _is_story_capture_url, timeout=10 if click.get("ok") else 4
            )
            if click.get("ok") and not payloads:
                time.sleep(1.5)
                payloads.extend(
                    _cdp_collect_json_payloads(session, _is_story_capture_url, timeout=6)
                )
            for payload in payloads:
                packed = _story_items_from_feed_payload(payload)
                if not packed:
                    packed = normalize_items(payload)
                items.extend(item for item in packed if isinstance(item, dict))
    except Exception as exc:
        return None, f"browser_story: {exc}"

    if items:
        return items, "browser story viewer"
    return None, "browser_story: no story payloads"


# Captcha / slide-verify selectors and text fragments that Douyin injects when
# it suspects automated access.  Checked via CDP Runtime.evaluate.
_CAPTCHA_DOM_EXPRESSION = """
(() => {
    // URL-based detection
    if (location.href.includes('verify.douyin.com') || location.href.includes('/captcha/')) return 'url';
    // DOM selectors used by Douyin's verify SDK.
    // Only count VISIBLE elements: Douyin pre-loads a hidden captcha iframe
    // (display:none, 0x0) on every page; matching it causes false positives.
    // FIX-5.2: Removed overly broad '[class*="captcha"]' which matches
    // pre-loaded config containers (captcha-config-wrapper, captcha-preload).
    // FIX-CAPTCHA: Removed iframe[src*="verify"] (preloaded SDK iframe on every page)
    // and .verify-wrap (collapsed container). Added size threshold >200x150 to only
    // match actual visible captcha overlays, not tiny preloaded SDK elements.
    const selectors = [
        '#captcha-verify-image',
        '.captcha_verify_container',
        '.captcha-verify-container',
        '#captcha_verify_image',
        '#verify-container',
    ];
    for (const sel of selectors) {
        const els = document.querySelectorAll(sel);
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            // FIX-AUDIT-4: Added viewport + z-index checks to avoid matching
            // off-screen or behind-content elements (e.g. preloaded SDK containers).
            if (rect.width > 200 && rect.height > 150
                && rect.top < window.innerHeight
                && rect.bottom > 0
                && style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && parseInt(style.zIndex || '0') >= 10) {
                return 'selector:' + sel;
            }
        }
    }
    // Text-based detection (visible body text)
    const bodyText = (document.body && document.body.innerText) || '';
    // FIX-AUDIT-2: Removed \u5b89\u5168\u9a8c\u8bc1 (appears in normal Douyin UI footer/settings)
    const markers = ['\u8bf7\u5b8c\u6210\u9a8c\u8bc1', '\u62d6\u52a8\u6ed1\u5757', '\u8bf7\u5b8c\u6210\u4e0a\u65b9\u9a8c\u8bc1'];
    for (const m of markers) {
        if (bodyText.includes(m)) return 'text:' + m;
    }
    return '';
})()
"""


def _detect_browser_captcha(session):
    """Return a truthy captcha-reason string if the CDP page shows a captcha."""
    try:
        result = session.call(
            "Runtime.evaluate",
            {"expression": _CAPTCHA_DOM_EXPRESSION, "returnByValue": True},
            timeout=8,
        )
        value = (result.get("result") or {}).get("value") or ""
        return str(value)
    except Exception:
        return ""


def fetch_posts_via_browser(profile, sec_user_id, limit=0, progress_callback=None, cdp_url=None):
    """
    Capture profile works by reading the browser's own /aweme/post/ responses.

    Plain HTTP clients get HTTP 200 with an empty body (Argus). A real Edge/Chrome
    page signs requests correctly; we never re-issue those URLs ourselves.
    """
    launched = None
    if not cdp_url:
        launched = ensure_media_fetch_browser()
        cdp_url = launched["cdp_url"]
    elif not cdp_is_available(cdp_url):
        launched = ensure_media_fetch_browser()
        cdp_url = launched["cdp_url"]

    page = _cdp_pick_page(cdp_url, prefer_douyin=False)
    if not page:
        # Open a blank page target.
        try:
            with httpx.Client(trust_env=False) as client:
                client.put(cdp_url.rstrip("/") + "/json/new?about:blank", timeout=5)
        except Exception:
            pass
        time.sleep(0.4)
        page = _cdp_pick_page(cdp_url, prefer_douyin=False)
    if not page:
        raise RuntimeError("Media fetch browser has no open page target")

    items = []
    pages = 0
    profile_url = f"https://www.douyin.com/user/{sec_user_id}"
    with CdpSession(page["webSocketDebuggerUrl"], timeout=45) as session:
        session.call("Network.enable", {"maxResourceBufferSize": 50 * 1024 * 1024})
        session.call("Page.enable", {})
        # FIX-LOGIN: Pre-apply session cookies so login-gated profiles
        # (notes, images, restricted works) load correctly on first navigation.
        _pre_cookie = (profile.get("cookies") or "").strip() or _mobile_cookie_header()
        _pre_applied = 0
        if _pre_cookie:
            _pre_applied = _cdp_apply_session_cookies(session, _pre_cookie)
            if _pre_applied:
                logging.debug("Pre-applied %d session cookies before profile navigation.", _pre_applied)
        session.call("Page.navigate", {"url": profile_url}, timeout=30)

        # ?? Captcha gate: check before waiting for API payloads ??
        # FIX-AUDIT-3: Wait for SPA to render before checking captcha.
        # Pre-loaded SDK elements are briefly visible right after navigate.
        time.sleep(2)
        captcha_reason = _detect_browser_captcha(session)
        if captcha_reason:
            logging.warning("Captcha detected on initial load (reason: %s)", captcha_reason)
            # Give the page a moment in case the captcha is transient, then retry.
            time.sleep(4)  # OPT-A: reduced from 12s; circuit breaker handles cycle skip
            session.call("Page.reload", {"ignoreCache": True}, timeout=20)
            time.sleep(2)  # OPT-A: reduced from 5s
            captcha_reason = _detect_browser_captcha(session)
            if captcha_reason:
                logging.warning("Captcha confirmed after reload (reason: %s)", captcha_reason)
                reset_media_fetch_browser()
                raise CaptchaDetectedError(
                    f"Douyin is showing a captcha/verification challenge ({captcha_reason}). "
                    "The fetch browser was restarted with a clean session."
                )

        # First post page usually fires shortly after SPA boot.
        payloads = _cdp_collect_post_payloads(session, sec_user_id, timeout=18)  # OPT-B: reduced from 28s
        if not payloads:
            # Check captcha again - it may appear after the initial page load.
            captcha_reason = _detect_browser_captcha(session)
            if captcha_reason:
                logging.warning("Captcha detected after empty payloads (reason: %s)", captcha_reason)
                reset_media_fetch_browser()
                raise CaptchaDetectedError(
                    f"Douyin showed a captcha instead of profile data ({captcha_reason}). "
                    "The fetch browser was restarted with a clean session."
                )
            # Retry once: hard reload after cookies settle.
            session.call("Page.reload", {"ignoreCache": True}, timeout=20)
            payloads = _cdp_collect_post_payloads(session, sec_user_id, timeout=15)  # OPT-C: reduced from 25s
        if not payloads:
            # Final captcha check before giving up.
            captcha_reason = _detect_browser_captcha(session)
            if captcha_reason:
                logging.warning("Captcha detected on final check (reason: %s)", captcha_reason)
                reset_media_fetch_browser()
                raise CaptchaDetectedError(
                    f"Douyin showed a captcha instead of profile data ({captcha_reason}). "
                    "The fetch browser was restarted with a clean session."
                )
            raise EmptyApiResponseError(
                "Browser profile page never produced an /aweme/post/ response body. "
                "Douyin did not produce a browser-signed public video list."
            )

        seen_ids = set()
        cursor_guard = set()
        session_applied = False
        while payloads:
            data = payloads.pop(0)
            page_items = normalize_items(data)
            # Anonymous responses can contain both a valid aweme_list and a
            # not_login_module used only for the page's login promotion. The
            # promotion is not a hard API failure when usable works are present.
            if response_has_login_tip(data) and not page_items:
                cookie_header = (profile.get("cookies") or "").strip() or _mobile_cookie_header()
                if cookie_header and not session_applied:
                    applied = _cdp_apply_session_cookies(session, cookie_header)
                    if applied:
                        session_applied = True
                        session.call("Page.reload", {"ignoreCache": True}, timeout=20)
                        payloads = _cdp_collect_post_payloads(session, sec_user_id, timeout=15)  # OPT-G: reduced from 25s
                        if payloads:
                            continue
                raise LoginRequiredError(
                    "Douyin hides this profile's works when logged out. Use Douyin Login for notes, images, or restricted works."
                )
            for item in page_items:
                aweme_id = str(item.get("aweme_id") or "")
                if not aweme_id:  # FIX-3.3
                    aweme_id = str(item.get("group_id") or "")
                if aweme_id and aweme_id in seen_ids:
                    continue
                if aweme_id:
                    seen_ids.add(aweme_id)
                items.append(item)
            pages += 1
            report_progress(
                progress_callback,
                phase="scanning",
                media_kind="video",
                pages=pages,
                found=len(items),
                source="browser",
            )
            # FIX-5.3: Cap scroll pagination at 50 pages (~900 items) to prevent
            # indefinite scrolling that blocks other profiles for 50+ minutes.
            if pages >= 50:
                logging.info("Scroll pagination capped at 50 pages (%d items) for %s.", len(items), profile.get("name"))
                break
            if limit and len(items) >= limit:
                _record_media_browser_success(cdp_url)
                return items[:limit]
            has_more = bool(int(data.get("has_more") or 0)) if isinstance(data, dict) else False
            next_cursor = int(data.get("max_cursor") or 0) if isinstance(data, dict) else 0
            if not has_more:
                break
            if next_cursor in cursor_guard:
                break
            cursor_guard.add(next_cursor)
            # Scroll the profile grid to trigger the next signed page request.
            try:
                session.call(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "window.scrollTo(0, document.body.scrollHeight || "
                            "document.documentElement.scrollHeight || 0);"
                        ),
                        "returnByValue": True,
                    },
                    timeout=10,
                )
            except Exception:
                pass
            more = _cdp_collect_post_payloads(session, sec_user_id, timeout=12)
            # Prefer payloads that advance the cursor.
            advanced = []
            for payload in more:
                try:
                    cursor = int(payload.get("max_cursor") or 0)
                except (TypeError, ValueError):
                    cursor = 0
                if cursor and cursor not in cursor_guard:
                    advanced.append(payload)
            payloads.extend(advanced or more)
            if not payloads:
                # P2: captcha may appear mid-scroll on deep pagination.
                scroll_captcha = _detect_browser_captcha(session)
                if scroll_captcha:
                    logging.warning("Captcha detected during scroll pagination (reason: %s)", scroll_captcha)
                    reset_media_fetch_browser()
                    raise CaptchaDetectedError(
                        f"Douyin injected a captcha during scroll pagination ({scroll_captcha}). "
                        f"Collected {len(items)} items before the block. "
                        "The fetch browser was restarted with a clean session."
                    )
                break
    if items:
        _record_media_browser_success(cdp_url)

    # OPT-3: Clear the page so the next profile starts from a neutral state.
    # This reduces cross-profile fingerprinting and stale-captcha carry-over.
    try:
        session.call("Page.navigate", {"url": "about:blank"}, timeout=5)
    except Exception:
        pass

    return items


def import_chrome_session(cdp_url=DEFAULT_CHROME_CDP):
    endpoint = cdp_url.rstrip("/") + "/json/list"
    with httpx.Client(trust_env=False) as client:
        response = client.get(endpoint, timeout=10)
    response.raise_for_status()
    targets = response.json()
    target = next(
        (
            item
            for item in targets
            if item.get("type") == "page"
            and str(item.get("url") or "").startswith("https://www.douyin.com/")
            and item.get("webSocketDebuggerUrl")
        ),
        None,
    )
    if not target:
        raise RuntimeError("Open a logged-in https://www.douyin.com/ tab in Chrome first")
    result = chrome_cdp_command(target["webSocketDebuggerUrl"], "Network.getAllCookies")
    now = time.time()
    cookies = []
    for item in result.get("cookies") or []:
        name = str(item.get("name") or "")
        domain = str(item.get("domain") or "")
        expires = float(item.get("expires") or 0)
        if not name or not (domain == "douyin.com" or domain.endswith(".douyin.com")):
            continue
        if expires > 0 and expires < now:
            continue
        cookies.append(item)
    required = _SESSION_REQUIRED_COOKIES
    names = {str(item.get("name") or "") for item in cookies}
    if not names.intersection(required):
        raise RuntimeError("Chrome's Douyin tab is not logged in")
    cookies.sort(
        key=lambda item: (
            -len(str(item.get("path") or "/")),
            0 if item.get("domain") == "www.douyin.com" else 1,
            str(item.get("name") or ""),
        )
    )
    cookie_header = "; ".join(f"{item['name']}={item.get('value', '')}" for item in cookies)
    save_session_cookie_header(cookie_header, cdp_url)
    save_mobile_session_cookie_header(cookie_header, source="edge-qr-app")
    _persistent_mobile_device()
    return {
        "cookie_count": len(cookies),
        "source": cdp_url,
        "saved_to": str(SESSION_FILE),
        "app_session": str(MOBILE_SESSION_FILE),
        "app_capable": True,
    }


def apply_saved_session(profile):
    profile = dict(profile)
    if not (profile.get("cookies") or "").strip():
        profile["cookies"] = _mobile_cookie_header()
    return profile


def saved_session_info():
    promote_web_session_to_app()
    mobile_header = load_mobile_session_cookie_header()
    web_header = load_session_cookie_header()
    cookie_header = mobile_header or web_header
    session = load_json(MOBILE_SESSION_FILE if mobile_header else SESSION_FILE, {})
    names = _session_cookie_names(cookie_header)
    stored = load_json(MOBILE_DEVICE_FILE, {}) or {}
    device_id = str(stored.get("device_id") or "")
    install_id = str(stored.get("install_id") or "")
    logged_in = _session_is_logged_in(cookie_header)
    return {
        "logged_in": logged_in,
        "cookie_count": len(names),
        "imported_at": session.get("imported_at", "") if isinstance(session, dict) else "",
        "source": session.get("source", "") if isinstance(session, dict) else "",
        "app_capable": bool(logged_in and device_id and install_id),
    }


def clear_saved_session():
    """Remove DPAPI session files, device binding, and Chromium cookie jars."""
    SESSION_FILE.unlink(missing_ok=True)
    MOBILE_SESSION_FILE.unlink(missing_ok=True)
    MOBILE_DEVICE_FILE.unlink(missing_ok=True)
    _wipe_fetch_browser_cookie_store()


def _wipe_fetch_browser_cookie_store():
    """Best-effort wipe of plaintext Chromium cookies left by the login browser."""
    profile_root = FETCH_BROWSER_PROFILE_DIR
    if not profile_root.exists():
        return
    targets = [
        profile_root / "Default" / "Cookies",
        profile_root / "Default" / "Cookies-journal",
        profile_root / "Default" / "Network" / "Cookies",
        profile_root / "Default" / "Network" / "Cookies-journal",
        profile_root / "Default" / "Network" / "Cookies-encrypt",
    ]
    for path in targets:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logging.debug("Could not remove browser cookie file %s", path)


def find_browser_executable():
    candidates = []
    for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if not root:
            continue
        candidates.extend(
            [
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    for name in ("chrome.exe", "msedge.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Google Chrome or Microsoft Edge was not found")


def find_media_browser_executable():
    """Prefer Edge for public post capture; Chrome may not load Douyin's grid."""
    override = str(os.environ.get("DOUYIN_MEDIA_EDGE_PATH") or "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Configured Douyin media Edge executable was not found: {candidate}")
    candidates = []
    for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    found = shutil.which("msedge.exe")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Microsoft Edge is required for Douyin public-video capture; Chrome is intentionally not used because Douyin blocks its post-list request on this machine"
    )


def cdp_is_available(cdp_url):
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.get(cdp_url.rstrip("/") + "/json/version", timeout=2)
        return response.status_code == 200 and bool(response.json().get("webSocketDebuggerUrl"))
    except Exception:
        return False


def close_cdp_browser(cdp_url, timeout=10):
    endpoint = cdp_url.rstrip("/") + "/json/version"
    with httpx.Client(trust_env=False) as client:
        response = client.get(endpoint, timeout=5)
    response.raise_for_status()
    websocket_url = str(response.json().get("webSocketDebuggerUrl") or "")
    if not websocket_url:
        raise RuntimeError(f"Browser at {cdp_url} did not expose a debugging WebSocket")

    try:
        chrome_cdp_command(websocket_url, "Browser.close", timeout=3)
    except (OSError, RuntimeError, TimeoutError):
        # A successful Browser.close commonly drops the socket before replying.
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not cdp_is_available(cdp_url):
            return
        time.sleep(0.1)
    raise RuntimeError(f"Browser at {cdp_url} did not close in time")


def available_cdp_port(preferred=9223):
    preferred_url = f"http://127.0.0.1:{preferred}"
    if cdp_is_available(preferred_url):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return int(probe.getsockname()[1])
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])


def launch_douyin_login_browser():
    with MEDIA_BROWSER_LAUNCH_LOCK:
        browser_path = find_media_browser_executable()
        port = FETCH_BROWSER_CDP_PORT
        cdp_url = f"http://127.0.0.1:{port}"
        if cdp_is_available(cdp_url):
            close_cdp_browser(cdp_url)

        FETCH_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        command = [
            str(browser_path),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={FETCH_BROWSER_PROFILE_DIR}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "https://www.douyin.com/",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if cdp_is_available(cdp_url):
                return {
                    "process": process,
                    "cdp_url": cdp_url,
                    "browser_path": str(browser_path),
                    "profile_dir": str(FETCH_BROWSER_PROFILE_DIR),
                }
            if process.poll() is not None:
                raise RuntimeError(
                    "Microsoft Edge exited before the Douyin login connection was ready"
                )
            time.sleep(0.1)
        raise RuntimeError(
            f"Douyin login browser started but debugging port {port} never became ready"
        )


def print_console(text):
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write((str(text) + "\n").encode(encoding, errors="backslashreplace"))


def expand_portable_path(value):
    if not isinstance(value, str):
        return value
    replacements = {
        "${APP_DIR}": str(APP_DIR),
        "${PACK_ROOT}": str(PACK_ROOT),
        "${DOWNLOAD_ROOT}": str(ROOT_DOWNLOAD_DIR),
        "${TOOLS_DIR}": str(TOOLS_DIR),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return os.path.expandvars(value)


def map_config_strings(data, mapper):
    if isinstance(data, dict):
        return {key: map_config_strings(value, mapper) for key, value in data.items()}
    if isinstance(data, list):
        return [map_config_strings(value, mapper) for value in data]
    if isinstance(data, str):
        return mapper(data)
    return data


def load_json(path, fallback):
    if not path.exists():
        return fallback
    with open(path, "r", encoding="utf-8-sig") as fh:
        return map_config_strings(json.load(fh), expand_portable_path)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def safe_name(value, fallback="untitled"):
    text = str(value or "").strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:120]


def cookie_value(cookie_header, name):
    if not cookie_header:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def random_ms_token():
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(182)) + "=="


def default_query(profile, sec_user_id, cursor, count, *, path_kind="post"):
    cookie_header = profile.get("cookies", "") or ""
    ms_token = cookie_value(cookie_header, "msToken") or random_ms_token()
    verify_fp = cookie_value(cookie_header, "s_v_web_id") or cookie_value(cookie_header, "s_v_web_id".upper())
    uifid = cookie_value(cookie_header, "UIFID") or cookie_value(cookie_header, "uifid") or ""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": sec_user_id,
        "max_cursor": str(cursor),
        "locate_query": "false",
        "show_live_replay_strategy": "1",
        "need_time_list": "1",
        "time_list_query": "0",
        "whale_cut_token": "",
        "cut_version": "1",
        "count": str(count),
        "publish_video_strategy_type": "2",
        "from_user_page": "1",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "support_h265": "1",
        "support_dash": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1536",
        "screen_height": "864",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "139.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "139.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "16",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
        "uifid": uifid,
        "msToken": ms_token,
    }
    if verify_fp:
        params["verifyFp"] = verify_fp
        params["fp"] = verify_fp
    if path_kind == "story":
        params["cursor"] = str(cursor)
    return params


def signed_douyin_url(path, params):
    query = urllib.parse.urlencode(params)
    signed_query, _abogus, ua, _body = ABogus(
        fp=BrowserFingerprintGenerator.generate_fingerprint("Chrome"),
        user_agent=USER_AGENT,
        options=[0, 1, 8],  # GET request encoding
    ).generate_abogus(query, "")
    return f"https://www.douyin.com{path}?{signed_query}", ua


def response_has_login_tip(data):
    if not isinstance(data, dict):
        return False
    try:
        status_code = int(data.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code == 2483:
        return True
    module = data.get("not_login_module")
    return isinstance(module, dict) and bool(module.get("guide_login_tip_exist"))


def profile_has_active_story(client, profile, sec_user_id):
    """Web profile flag only. story_tab_empty=false means the 日常 tab exists,
    not that an unseen 24h story is live. Use /story/profile/list/ for that."""
    data = request_json(
        client,
        profile,
        "/aweme/v1/web/user/profile/other/",
        sec_user_id,
        0,
        1,
        path_kind="post",
    )
    if response_has_login_tip(data):
        raise LoginRequiredError("Douyin is asking this session to log in before showing stories")
    user = data.get("user") if isinstance(data, dict) else None
    return isinstance(user, dict) and user.get("story_tab_empty") is False


def extract_sec_uid_from_url(url):
    match = re.search(r"douyin\.com/user/([^/?#]+)", url or "")
    return urllib.parse.unquote(match.group(1)) if match else ""


async def resolve_profile_identity(profile):
    sec_uid = (
        extract_sec_uid_from_url(profile.get("original_profile_url", ""))
        or extract_sec_uid_from_url(profile.get("url", ""))
    )
    numeric_user_id = ""
    nickname = profile.get("name") or ""
    live_url = profile.get("fallback_live_url") or profile.get("url") or ""
    if not sec_uid and live_url and "live.douyin.com/" in live_url:
        # Never attach the logged-in session to room-enter probes. Douyin treats
        # authenticated /webcast/room/web/enter/ as concurrent live viewing and
        # kicks the user's real browser session from other rooms.
        live = DouyinLiveStream(
            proxy_addr=profile.get("proxy_addr") or None,
            cookies=None,
            stream_orientation=int(profile.get("stream_orientation") or 1),
        )
        raw = await live.fetch_web_stream_data(live_url, process_data=False)
        user = (raw.get("data", {}) or {}).get("user", {}) if isinstance(raw, dict) else {}
        sec_uid = sec_uid or user.get("sec_uid") or ""
        numeric_user_id = user.get("id_str") or ""
        nickname = user.get("nickname") or nickname
    if not sec_uid:
        raise RuntimeError("Could not resolve sec_user_id for this Douyin profile")
    return {"sec_user_id": sec_uid, "user_id": numeric_user_id, "nickname": nickname}


def iter_url_list(source):
    if isinstance(source, str):
        yield source
    elif isinstance(source, dict):
        values = source.get("url_list") or source.get("download_url_list") or []
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str):
                    yield item


def collect_video_urls(aweme):
    video = aweme.get("video") if isinstance(aweme, dict) else {}
    if not isinstance(video, dict):
        return []
    candidates = []
    for entry in video.get("bit_rate") or []:
        if isinstance(entry, dict):
            candidates.extend(iter_url_list(entry.get("play_addr")))
    for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
        candidates.extend(iter_url_list(video.get(key)))
    seen = set()
    deduped = []
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    deduped.sort(key=lambda item: (("watermark" in item.lower()), ("douyin.com/aweme/v1/play" in item), len(item)))
    return deduped


def collect_image_urls(aweme):
    if not isinstance(aweme, dict):
        return []
    images = aweme.get("images") or (aweme.get("image_post_info") or {}).get("images") or []
    result = []
    seen = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        candidates = list(iter_url_list(image))
        for key in ("display_image", "download_url", "owner_watermark_image", "thumbnail"):
            candidates.extend(iter_url_list(image.get(key)))
        candidates = [url for url in candidates if url and url not in seen]
        if not candidates:
            continue
        candidates.sort(
            key=lambda url: (
                "water" in url.lower(),
                not any(ext in url.lower().split("?", 1)[0] for ext in (".jpg", ".jpeg")),
                len(url),
            )
        )
        selected = candidates[0]
        seen.add(selected)
        result.append(selected)
    return result


def aweme_filename(aweme, suffix=".mp4"):
    # FIX-D2: Use millisecond timestamp to reduce collision risk for items
    # without aweme_id. Same-second collisions caused silent data loss.
    _fallback_id = str(aweme.get("aweme_id") or "")
    if not _fallback_id:  # FIX-3.3
        _fallback_id = str(aweme.get("group_id") or "")
    if not _fallback_id:
        import random as _rnd
        _fallback_id = f"ts{int(time.time() * 1000)}_{_rnd.randint(1000, 9999)}"
    # Strict id sanitize: alnum/_/- only so separators and ".." cannot escape
    # the download directory when the name is joined under target_dir.
    aweme_id = re.sub(r"[^A-Za-z0-9_-]+", "_", _fallback_id).strip("_") or "unknown"
    aweme_id = aweme_id[:64]
    # FIX-D1: Use (x or {}) to handle share_info being JSON null.
    desc = safe_name(aweme.get("desc") or (aweme.get("share_info") or {}).get("share_title") or aweme_id)
    # FIX-PATHLEN: Truncate description to prevent exceeding Windows MAX_PATH
    # (260 chars).  Base dir (~95) + timestamp (19) + id (19) + separators +
    # suffix + .part.PID overhead ? 150 chars reserved; cap desc at 80.
    if len(desc) > 80:
        desc = desc[:80].rstrip("_ ")
    try:
        stamp = datetime.fromtimestamp(int(aweme.get("create_time") or time.time())).strftime("%Y-%m-%d_%H-%M-%S")
    except (TypeError, ValueError, OSError):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{stamp}_{aweme_id}_{desc}{suffix}"


def load_state(output_dir):
    state_path = Path(output_dir) / "douyin_media_state.json"
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    legacy_ids = state.get("downloaded_aweme_ids") or []
    state.setdefault("downloaded_video_ids", list(legacy_ids))
    state.setdefault("downloaded_story_ids", [])
    return state_path, state


def save_state(state_path, state):
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json(state_path, state)



# FIX-6.5: Dynamic ttwid generation. The old hardcoded ttwid from Oct 2025
# expires server-side, causing all anonymous HTTP requests to fail.
_ttwid_cache = {"value": None, "expires": 0}
_ttwid_lock = threading.Lock()


def _get_fresh_ttwid():
    """Fetch a fresh ttwid cookie from ByteDance's registration endpoint.
    Cached for 30 minutes to avoid excessive requests."""
    now = time.time()
    if _ttwid_cache["value"] and _ttwid_cache["expires"] > now:
        return _ttwid_cache["value"]
    with _ttwid_lock:
        # Double-check after acquiring lock
        if _ttwid_cache["value"] and _ttwid_cache["expires"] > time.time():
            return _ttwid_cache["value"]
        try:
            resp = httpx.post(
                "https://ttwid.bytedance.com/ttwid/union/register/",
                json={
                    "region": "cn",
                    "aid": 1768,
                    "needFid": False,
                    "service": "www.ixigua.com",
                    "migrate_info": {"ticket": "", "source": "node"},
                    "cbUrlProtocol": "https",
                    "union": True,
                },
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # ttwid is returned as a Set-Cookie header
            for cookie_header in resp.headers.get_list("set-cookie"):
                if "ttwid=" in cookie_header:
                    # Extract ttwid value from "ttwid=VALUE; Path=/; ..."
                    match = re.search(r"ttwid=([^;]+)", cookie_header)
                    if match:
                        _ttwid_cache["value"] = match.group(1)
                        _ttwid_cache["expires"] = time.time() + 1800  # 30 min cache
                        logging.debug("Refreshed ttwid cookie (cached 30 min).")
                        return _ttwid_cache["value"]
            logging.warning("ttwid registration returned no ttwid cookie.")
        except Exception as exc:
            logging.warning("Failed to fetch fresh ttwid: %s", exc)
    return _ttwid_cache["value"]  # return stale if refresh failed


def _request_headers(profile, sec_user_id, ua, cookies, ms_token):
    headers = {
        "User-Agent": ua or USER_AGENT,
        "Referer": f"https://www.douyin.com/user/{sec_user_id}",
        "Origin": "https://www.douyin.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": '"Chromium";v="139", "Not=A?Brand";v="24", "Google Chrome";v="139"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if cookies:
        headers["Cookie"] = cookies
    else:
        # FIX-6.5: Use dynamically generated ttwid instead of hardcoded expired one.
        _ttwid = _get_fresh_ttwid()
        if _ttwid:
            headers["Cookie"] = f"ttwid={_ttwid}; msToken={ms_token}"
        else:
            headers["Cookie"] = f"msToken={ms_token}"
    return headers


def _session_authentication_status(client, cookies, ua):
    """Return True/False when Douyin confirms/rejects the session, else None."""
    if not cookies:
        return False
    try:
        response = client.get(
            "https://www.douyin.com/aweme/v1/web/query/user/?device_platform=webapp&aid=6383",
            headers={
                "User-Agent": ua or USER_AGENT,
                "Cookie": cookies,
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        if response.status_code != 200 or not response.content:
            return None
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        status_code = int(data.get("status_code"))
    except (TypeError, ValueError):
        return None
    if status_code == 0 and bool(data.get("id") or data.get("user_uid")):
        return True
    if status_code in {8, 12, 2483} or response_has_login_tip(data):
        return False
    # FIX-1.1: Log unrecognised status codes so new Douyin rejection
    # codes are visible in logs instead of silently falling through.
    if status_code != 0:
        logging.warning("query/user returned unrecognised status_code=%s", status_code)
    return None


def _parse_json_response(response):
    """Parse a Douyin API response, detecting captcha/HTML pages served with HTTP 200."""
    # Detect captcha / verification HTML pages that arrive with HTTP 200.
    raw_prefix = response.content[:512]
    content_type = (response.headers.get("content-type") or "").lower()
    if b"<html" in raw_prefix.lower() or b"<!doctype" in raw_prefix.lower() or "text/html" in content_type:
        body_text = response.text[:2000]
        # FIX-AUDIT-1: Removed bare "captcha" (matches normal SDK references like
        # captcha-config-wrapper, captcha-preload in every Douyin page), bare
        # "\u5b89\u5168\u9a8c\u8bc1" (appears in normal UI footer/settings), and "verify-wrap"
        # (collapsed container present on every page). Use specific markers only.
        if any(marker in body_text for marker in (
            "verify.douyin.com", "captcha_verify_container",
            "\u8bf7\u5b8c\u6210\u9a8c\u8bc1", "\u62d6\u52a8\u6ed1\u5757",
            "captcha-verify-image", "\u8bf7\u5b8c\u6210\u4e0a\u65b9\u9a8c\u8bc1",
        )):
            raise CaptchaDetectedError(
                "Douyin returned a captcha/verification page instead of API data. "
                "The session or IP has been flagged; wait and retry later."
            )
        raise RuntimeError(
            "Douyin returned an HTML page instead of a JSON API response "
            f"(HTTP {response.status_code}, content-type {content_type or 'unknown'})"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Douyin returned a non-JSON API response "
            f"(HTTP {response.status_code}, content-type {response.headers.get('content-type', 'unknown')})"
        ) from exc


def request_json(client, profile, path, sec_user_id, cursor=0, count=18, *, path_kind="post"):
    params = default_query(profile, sec_user_id, cursor, count, path_kind=path_kind)
    url, ua = signed_douyin_url(path, params)
    cookies = (profile.get("cookies") or "").strip() or _mobile_cookie_header()
    headers = _request_headers(profile, sec_user_id, ua, cookies, params["msToken"])

    # FIX-AUDIT-8: Removed unnecessary warm-up GET to douyin.com.
    # It added latency and could trigger rate limiting without benefit.
    # Session cookies are already sent with the actual API request.

    response = client.get(url, headers=headers)
    if response.status_code == 200 and not response.content:
        # Retry once with newly generated request parameters/signature. An empty
        # 200 can be transient, but Douyin does not document its precise cause.
        params = default_query(profile, sec_user_id, cursor, count, path_kind=path_kind)
        url, ua = signed_douyin_url(path, params)
        headers = _request_headers(profile, sec_user_id, ua, cookies, params["msToken"])
        response = client.get(url, headers=headers)

    if not response.content:
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(
                f"Douyin returned HTTP {response.status_code} with an empty API response"
            )
        if not cookies:
            raise LoginRequiredError(
                "Douyin returned an empty API response and no saved login session is available"
            )
        session_status = _session_authentication_status(client, cookies, ua)
        if session_status is False:
            raise LoginRequiredError(
                "Douyin rejected the saved login session; import it again before downloading videos"
            )
        if session_status is True:
            detail = "the saved session passed a separate account-session check"
        else:
            detail = "the saved session could not be independently verified"
        raise EmptyApiResponseError(
            "Douyin returned HTTP 200 with an empty API body after one retry; "
            f"{detail}. The cause is unconfirmed (request signing or client fingerprint rejection is possible). "
            "Live monitoring does not use this login."
        )

    response.raise_for_status()
    return _parse_json_response(response)


def _story_items_from_user_entry(entry):
    if not isinstance(entry, dict):
        return []
    items = []
    for key in STORY_NESTED_LIST_KEYS:
        value = entry.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    items.append(item)
        elif isinstance(value, dict) and (value.get("aweme_id") or value.get("group_id")):
            items.append(value)
    # Some packs nest under "user_story" / "life_story_info"
    for key in ("user_story", "life_story_info", "story_info"):
        nested = entry.get(key)
        if isinstance(nested, dict):
            items.extend(_story_items_from_user_entry(nested))
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    items.extend(_story_items_from_user_entry(item) or [item])
    # Attach author from pack user when story item lacks it
    pack_user = entry.get("user") or entry.get("author") or {}
    if isinstance(pack_user, dict) and pack_user:
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if not isinstance(author, dict) or not (author.get("sec_uid") or author.get("uid")):
                item["author"] = dict(pack_user)
    return items


def normalize_items(data):
    if not isinstance(data, dict):
        return []
    # Mobile life/feed packs stories per user.
    user_story_list = data.get("user_story_list")
    if isinstance(user_story_list, list):
        packed = []
        for entry in user_story_list:
            packed.extend(_story_items_from_user_entry(entry))
        if packed:
            return packed
        # Explicit empty pack is still a valid "no stories" response.
        if user_story_list is not None and "user_story_list" in data:
            return []
    active = data.get("active_data")
    if isinstance(active, dict):
        packed = []
        for key in ("data", "aweme_list", "item_list"):
            nested = active.get(key)
            if isinstance(nested, list):
                packed.extend(
                    item.get("aweme") if isinstance(item, dict) and isinstance(item.get("aweme"), dict) and not item.get("aweme_id") else item
                    for item in nested
                    if isinstance(item, dict)
                )
        if packed:
            return packed
    for key in ("aweme_list", "items", "story_list", "moment_list", "data"):
        value = data.get(key)
        if isinstance(value, list):
            # follow/familiar sometimes wrap aweme under {"aweme": {...}}
            unwrapped = []
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("aweme"), dict) and not item.get("aweme_id"):
                    unwrapped.append(item["aweme"])
                else:
                    unwrapped.append(item)
            return unwrapped
    return []


def resolve_numeric_user_id(client, profile, sec_user_id):
    """Best-effort numeric uid for life/feed packing."""
    try:
        data = request_json(
            client, profile, "/aweme/v1/web/user/profile/other/", sec_user_id, 0, 1, path_kind="post"
        )
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict) and user.get("uid"):
            return str(user["uid"])
    except Exception:
        pass
    return ""


def request_life_feed(client, profile, sec_user_id, user_id=""):
    """
    Query mobile life/feed for active stories of one user.

    Returns (data_dict_or_None, source_label). status_code 0 with null/empty
    user_story_list means the endpoint works but nobody in the pack has an
    open story ring right now.
    """
    cookies = (profile.get("cookies") or "").strip()
    if not cookies:
        return None, f"{LIFE_FEED_PATH}: missing cookies"
    numeric_uid = str(user_id or "").strip()
    if not numeric_uid.isdigit():
        numeric_uid = resolve_numeric_user_id(client, profile, sec_user_id)
    if not numeric_uid.isdigit():
        return None, f"{LIFE_FEED_PATH}: could not resolve numeric user_id"

    params = default_query(profile, sec_user_id, 0, 20, path_kind="story")
    # life/feed is an app endpoint; drop web-only sec_user_id from query noise is fine
    body_variants = (
        {
            "user_ids": json.dumps([int(numeric_uid)]),
            "sec_user_ids": json.dumps([sec_user_id]),
            "count": "20",
            "cursor": "0",
            "pull_type": "2",
        },
        {
            "user_ids": json.dumps([int(numeric_uid)]),
            "count": "20",
            "cursor": "0",
        },
        {
            "user_ids": numeric_uid,
            "count": "20",
            "cursor": "0",
        },
    )
    last_message = f"{LIFE_FEED_PATH}: no response"
    last_ok = None
    for host in LIFE_FEED_HOSTS:
        for body in body_variants:
            body_text = urllib.parse.urlencode(body)
            query = urllib.parse.urlencode(params)
            try:
                signed_query, _ab, ua, _body = ABogus(
                    fp=BrowserFingerprintGenerator.generate_fingerprint("Chrome"),
                    user_agent=USER_AGENT,
                ).generate_abogus(query, body_text)
            except Exception as exc:
                last_message = f"{LIFE_FEED_PATH}: sign failed: {exc}"
                continue
            headers = {
                "User-Agent": ua,
                "Cookie": cookies,
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            try:
                response = client.post(
                    f"{host}{LIFE_FEED_PATH}?{signed_query}",
                    headers=headers,
                    content=body_text.encode("utf-8"),
                )
            except Exception as exc:
                last_message = f"{host}{LIFE_FEED_PATH}: {exc}"
                continue
            if not response.content:
                last_message = f"{host}{LIFE_FEED_PATH}: empty body"
                continue
            try:
                data = response.json()
            except Exception as exc:
                last_message = f"{host}{LIFE_FEED_PATH}: bad json ({exc})"
                continue
            if not isinstance(data, dict):
                last_message = f"{host}{LIFE_FEED_PATH}: non-object response"
                continue
            try:
                status_code = int(data.get("status_code") or 0)
            except (TypeError, ValueError):
                status_code = -1
            if status_code != 0:
                last_message = (
                    f"{host}{LIFE_FEED_PATH}: status_code={status_code} "
                    f"{(data.get('status_msg') or data.get('message') or '')}"
                )
                continue
            # Accepted schema even when user_story_list is null (no open rings).
            last_ok = data
            items = normalize_items(data)
            if items:
                return data, f"{host}{LIFE_FEED_PATH}"
            last_message = f"{LIFE_FEED_PATH}: no active visible stories"
    if last_ok is not None:
        return last_ok, last_message
    return None, last_message


# Minimum plausible file sizes to catch truncated / corrupt downloads.
_MIN_VIDEO_BYTES = 2048       # 2 KB ? real videos are always larger
_MIN_IMAGE_BYTES = 512        # 512 B ? real images are always larger
# MP4 / ISO-BMG files start with a box whose type is at offset 4.
_MP4_FTYP_SIGNATURES = (b"ftyp", b"moov", b"mdat", b"free", b"skip")


def _verify_downloaded_file(output_path):
    """Raise ValueError if a freshly downloaded file looks truncated or corrupt."""
    size = output_path.stat().st_size
    suffix = output_path.suffix.lower()
    if suffix in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
        if size < _MIN_VIDEO_BYTES:
            raise ValueError(
                f"Downloaded video is only {size} bytes ? likely truncated"
            )
        # Check ISO base media container header (first 12 bytes).
        with open(output_path, "rb") as fh:
            header = fh.read(12)
        if suffix in (".mp4", ".mov", ".m4v") and len(header) >= 8:
            box_type = header[4:8]
            if box_type not in _MP4_FTYP_SIGNATURES:
                raise ValueError(
                    f"Downloaded file does not look like a valid MP4 "
                    f"(box type {box_type!r})"
                )
        # FIX-6.4: Add EBML magic-byte check for WebM/MKV.
        if suffix in (".webm", ".mkv") and len(header) >= 4:
            if header[:4] != b"\x1a\x45\xdf\xa3":
                raise ValueError(
                    "Downloaded file does not have a WebM/MKV EBML header"
                )
    elif suffix in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        if size < _MIN_IMAGE_BYTES:
            raise ValueError(
                f"Downloaded image is only {size} bytes ? likely truncated"
            )
        with open(output_path, "rb") as fh:
            magic = fh.read(12)
        if suffix in (".jpg", ".jpeg") and magic[:2] != b"\xff\xd8":
            # Douyin's mobile CDN often serves HEIC content regardless of the
            # requested extension.  Accept any known image container so valid
            # downloads are not silently deleted.
            _known_image_magics = (
                b"\xff\xd8",          # JPEG
                b"\x89PNG",            # PNG
                b"RIFF",                # WebP (RIFF....WEBP)
                b"\x00\x00\x00",     # HEIC/AVIF (ftyp box, size prefix)
            )
            if not any(magic[:len(m)] == m for m in _known_image_magics):
                # Also accept HEIC where 'ftyp' appears at offset 4
                if len(magic) >= 8 and magic[4:8] != b"ftyp":
                    raise ValueError("Downloaded file does not have a known image header")
        if suffix == ".png" and magic[:4] != b"\x89PNG":
            raise ValueError("Downloaded file does not have a PNG header")
        # FIX-6.4: Add magic-byte checks for WebP (RIFF....WEBP).
        if suffix == ".webp" and len(magic) >= 12:
            if magic[:4] != b"RIFF" or magic[8:12] != b"WEBP":
                raise ValueError("Downloaded file does not have a WebP header")


def download_bytes(client, url, output_path, progress_callback=None, progress_details=None):
    # FIX-6.3: Use PID-unique .part suffix to prevent concurrent download corruption.
    import os as _os_mod
    part_path = output_path.with_suffix(output_path.suffix + f".part.{_os_mod.getpid()}")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-",
    }
    with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        try:
            total_bytes = int(response.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            total_bytes = 0
        details = dict(progress_details or {})
        report_progress(
            progress_callback,
            phase="downloading",
            bytes_downloaded=0,
            bytes_total=total_bytes,
            **details,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded_bytes = 0
        reported_bytes = 0
        with open(part_path, "wb") as fh:
            for chunk in response.iter_bytes(1024 * 1024):
                if chunk:
                    fh.write(chunk)
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes - reported_bytes >= 4 * 1024 * 1024:
                        report_progress(
                            progress_callback,
                            phase="downloading",
                            bytes_downloaded=downloaded_bytes,
                            bytes_total=total_bytes,
                            **details,
                        )
                        reported_bytes = downloaded_bytes
        if downloaded_bytes != reported_bytes:
            report_progress(
                progress_callback,
                phase="downloading",
                bytes_downloaded=downloaded_bytes,
                bytes_total=total_bytes,
                **details,
            )
        # FIX-6.1: Detect silent truncation when server closes early.
        if total_bytes > 0 and downloaded_bytes != total_bytes:
            part_path.unlink(missing_ok=True)
            raise IOError(
                f"Download truncated: got {downloaded_bytes} of {total_bytes} bytes"
            )
        # FIX-6.2: Detect zero-byte writes (disk full or permission error).
        if downloaded_bytes == 0 and total_bytes == 0:
            try:
                if part_path.stat().st_size == 0:
                    part_path.unlink(missing_ok=True)
                    raise IOError("Download produced 0 bytes (possible disk full or blocked CDN)")
            except OSError:
                pass
        part_path.replace(output_path)
        try:
            _verify_downloaded_file(output_path)
        except ValueError:
            output_path.unlink(missing_ok=True)
            raise


@dataclass
class MediaResult:
    status: str = "ok"
    checked: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    message: str = ""
    files: list[str] = field(default_factory=list)



def _extract_video_id(aweme):
    """Extract video_id from aweme data for play-API fallback downloads."""
    import re as _re
    video = aweme.get("video") if isinstance(aweme, dict) else {}
    if not isinstance(video, dict):
        return None
    # Try play_addr url_list first
    for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
        addr = video.get(key) or {}
        for url in (addr.get("url_list") or []):
            m = _re.search(r"video_id=([a-zA-Z0-9]+)", url)
            if m:
                return m.group(1)
    # Try bit_rate entries
    for entry in video.get("bit_rate") or []:
        if isinstance(entry, dict):
            addr = entry.get("play_addr") or {}
            for url in (addr.get("url_list") or []):
                m = _re.search(r"video_id=([a-zA-Z0-9]+)", url)
                if m:
                    return m.group(1)
    # Try uri field directly
    uri = video.get("vid") or video.get("uri") or ""
    if uri and _re.match(r"^v[0-9a-f]{4}", uri):
        return uri
    return None


def download_aweme_items(
    client,
    profile,
    items,
    output_dir,
    state,
    media_kind,
    progress_callback=None,
):
    result = MediaResult(checked=len(items))
    state_key = "downloaded_story_ids" if media_kind == "story" else "downloaded_video_ids"
    downloaded_ids = set(str(item) for item in state.get(state_key, []))
    default_target_dir = Path(output_dir) / ("stories" if media_kind == "story" else "videos")
    total_items = len(items)
    report_progress(
        progress_callback,
        phase="downloading",
        media_kind=media_kind,
        current=0,
        total=total_items,
        downloaded=0,
        skipped=0,
        failed=0,
    )
    for item_index, aweme in enumerate(items, start=1):
        if not isinstance(aweme, dict):
            continue
        aweme_id = str(aweme.get("aweme_id") or "")
        if not aweme_id:  # FIX-3.3: only fall back to group_id when aweme_id is truly absent
            aweme_id = str(aweme.get("group_id") or "")
        if aweme_id and aweme_id in downloaded_ids:
            result.skipped += 1
            if result.skipped % 25 == 0 or item_index == total_items:
                report_progress(
                    progress_callback,
                    phase="downloading",
                    media_kind=media_kind,
                    current=item_index,
                    total=total_items,
                    downloaded=result.downloaded,
                    skipped=result.skipped,
                    failed=result.failed,
                )
            continue
        image_urls = collect_image_urls(aweme)
        # Image works (notably aweme_type 68) expose their BGM through
        # video.play_addr. Prefer the actual images instead of saving that audio
        # response with an .mp4 extension.
        video_urls = [] if image_urls else collect_video_urls(aweme)
        local_media = Path(str(aweme.get("_local_media_path") or ""))
        if not video_urls and not image_urls and not (local_media.is_file() and local_media.stat().st_size > 0):
            result.failed += 1
            report_progress(
                progress_callback,
                phase="downloading",
                media_kind=media_kind,
                current=item_index,
                total=total_items,
                downloaded=result.downloaded,
                skipped=result.skipped,
                failed=result.failed,
            )
            continue
        target_dir = (
            Path(output_dir) / "images"
            if media_kind != "story" and image_urls
            else default_target_dir
        )
        output_path = target_dir / aweme_filename(aweme)
        item_label = safe_name(
            aweme.get("desc")
            or (aweme.get("share_info") or {}).get("share_title")
            or aweme_id
            or output_path.stem
        )[:56]
        if output_path.exists() and output_path.stat().st_size > 0:
            result.skipped += 1
            if aweme_id:
                downloaded_ids.add(aweme_id)
            if result.skipped % 25 == 0 or item_index == total_items:
                report_progress(
                    progress_callback,
                    phase="downloading",
                    media_kind=media_kind,
                    current=item_index,
                    total=total_items,
                    downloaded=result.downloaded,
                    skipped=result.skipped,
                    failed=result.failed,
                )
            continue
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        saved = False
        saved_files = []
        local_media = Path(str(aweme.get("_local_media_path") or ""))
        if local_media.is_file() and local_media.stat().st_size > 0:
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_media, output_path)
                saved = output_path.stat().st_size > 0
                if saved:
                    saved_files.append(output_path)
            except OSError:
                saved = False
        if not saved and video_urls:
            candidates = video_urls[:8]
            for attempt, url in enumerate(candidates, start=1):
                details = {
                    "media_kind": media_kind,
                    "current": item_index,
                    "total": total_items,
                    "downloaded": result.downloaded,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "item": item_label,
                    "attempt": attempt,
                    "attempts": len(candidates),
                }
                try:
                    download_bytes(
                        client,
                        url,
                        output_path,
                        progress_callback=progress_callback,
                        progress_details=details,
                    )
                    saved = True
                    saved_files.append(output_path)
                    break
                except Exception as exc:
                    # FIX-6.3b: Clean up PID-suffixed .part files
                    for _pf in output_path.parent.glob(output_path.name + ".part.*"):
                        _pf.unlink(missing_ok=True)
                    report_progress(
                        progress_callback,
                        phase="retrying",
                        error=type(exc).__name__,
                        **details,
                    )
                    continue
            # FIX-CDN-FALLBACK: When all CDN candidates return 0 bytes,
            # try the play API directly using the video_id.  Some videos'
            # CDN edge nodes silently block body data while returning valid
            # content-length headers.  The /aweme/v1/play/ endpoint always
            # serves the full file regardless of CDN node issues.
            # Uses a FRESH httpx client to avoid connection-pool poisoning
            # from the failed CDN streams.
            if not saved:
                _vid = _extract_video_id(aweme)
                if _vid:
                    _play_url = f"https://api.amemv.com/aweme/v1/play/?video_id={_vid}&line=0"
                    logging.info(
                        "CDN fallback: trying play API for %s (video_id=%s)",
                        item_label, _vid,
                    )
                    _fb_details = {
                        "media_kind": media_kind,
                        "current": item_index,
                        "total": total_items,
                        "downloaded": result.downloaded,
                        "skipped": result.skipped,
                        "failed": result.failed,
                        "item": item_label,
                        "attempt": len(candidates) + 1,
                        "attempts": len(candidates) + 1,
                    }
                    try:
                        _fb_timeout = httpx.Timeout(30, connect=10, read=20, write=20, pool=20)
                        with httpx.Client(
                            timeout=_fb_timeout, follow_redirects=True, http2=False,
                        ) as _fb_client:
                            download_bytes(
                                _fb_client,
                                _play_url,
                                output_path,
                                progress_callback=progress_callback,
                                progress_details=_fb_details,
                            )
                        saved = True
                        saved_files.append(output_path)
                        logging.info("CDN fallback succeeded for %s via play API.", item_label)
                    except Exception as _fb_exc:
                        for _pf in output_path.parent.glob(output_path.name + ".part.*"):
                            _pf.unlink(missing_ok=True)
                        logging.warning(
                            "CDN fallback play API also failed for %s: %s: %s",
                            item_label, type(_fb_exc).__name__, _fb_exc,
                        )
        else:
            stem = target_dir / aweme_filename(aweme, suffix="")
            _image_failures = 0  # FIX-3.2: track partial image set failures
            for index, url in enumerate(image_urls, start=1):
                image_path = Path(f"{stem}_{index:02d}.jpg")
                details = {
                    "media_kind": media_kind,
                    "current": item_index,
                    "total": total_items,
                    "downloaded": result.downloaded,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "item": item_label,
                    "attempt": index,
                    "attempts": len(image_urls),
                }
                try:
                    download_bytes(
                        client,
                        url,
                        image_path,
                        progress_callback=progress_callback,
                        progress_details=details,
                    )
                    saved = True
                    saved_files.append(image_path)
                except Exception as exc:
                    _image_failures += 1
                    for _pf in image_path.parent.glob(image_path.name + ".part.*"):
                        _pf.unlink(missing_ok=True)
                    report_progress(
                        progress_callback,
                        phase="retrying",
                        error=type(exc).__name__,
                        **details,
                    )
            # FIX-3.2: If any images failed, don't mark the post as fully downloaded
            # so failed images get retried on the next check cycle.
            if _image_failures > 0:
                logging.warning(
                    "Image set %s: %d/%d images failed; will retry next cycle.",
                    item_label, _image_failures, len(image_urls),
                )
                saved = False  # prevent marking as downloaded
        if not saved:
            result.failed += 1
            report_progress(
                progress_callback,
                phase="downloading",
                media_kind=media_kind,
                current=item_index,
                total=total_items,
                downloaded=result.downloaded,
                skipped=result.skipped,
                failed=result.failed,
                item=item_label,
            )
            continue
        meta_path = saved_files[0].with_suffix(saved_files[0].suffix + ".json")
        save_json(meta_path, aweme)
        result.downloaded += 1
        result.files.extend(str(path) for path in saved_files)
        if aweme_id:
            downloaded_ids.add(aweme_id)
        # FIX-3.1: Periodic state save every 10 downloads to prevent full
        # re-download if the process crashes mid-loop.
        if result.downloaded % 10 == 0:
            state[state_key] = sorted(downloaded_ids)
            _periodic_state_path = Path(output_dir) / "douyin_media_state.json"
            save_state(_periodic_state_path, state)
        report_progress(
            progress_callback,
            phase="downloading",
            media_kind=media_kind,
            current=item_index,
            total=total_items,
            downloaded=result.downloaded,
            skipped=result.skipped,
            failed=result.failed,
            item=item_label,
        )
    state[state_key] = sorted(downloaded_ids)
    if media_kind != "story":
        state["downloaded_aweme_ids"] = sorted(downloaded_ids)
    return result


def fetch_posts(client, profile, sec_user_id, limit=0, progress_callback=None):
    items = []
    cursor = 0
    pages = 0
    while True:
        data = request_json(client, profile, POST_PATH, sec_user_id, cursor, 18, path_kind="post")
        page_items = normalize_items(data)
        # FIX-5.1: Only raise LoginRequiredError when there are NO items.
        # Douyin anonymous responses often include both a login-prompt object
        # AND a valid aweme_list. Aborting when items exist is a false positive.
        if response_has_login_tip(data) and not page_items:
            raise LoginRequiredError("Douyin is asking this session to log in before showing latest works")
        items.extend(page_items)
        pages += 1
        report_progress(
            progress_callback,
            phase="scanning",
            media_kind="video",
            pages=pages,
            found=len(items),
        )
        if limit and len(items) >= limit:
            return items[:limit]
        has_more = bool(int(data.get("has_more") or 0)) if isinstance(data, dict) else False
        next_cursor = int(data.get("max_cursor") or 0) if isinstance(data, dict) else 0
        if not has_more or next_cursor == cursor:
            return items
        cursor = next_cursor


def item_author_sec_uid(item):
    if not isinstance(item, dict):
        return ""
    author = item.get("author") or item.get("user") or {}
    if isinstance(author, dict):
        value = author.get("sec_uid") or author.get("sec_user_id")
        if value:
            return str(value)
    return str(item.get("sec_uid") or item.get("sec_user_id") or "")


def is_time_limited_story(item):
    if not isinstance(item, dict):
        return False
    # Author-level story_ttl/story_open are profile flags, not content markers.
    # Only treat the item itself as a time-limited story.
    for key in (
        "is_story",
        "is_moment",
        "is_25_story",
        "is_24_story",
        "is_moment_story",
        "story_ttl",
        "story_expire_time",
        "moment_id",
    ):
        value = item.get(key)
        if value not in (None, False, 0, "", [], {}):
            # story_ttl on author objects leaks into items via author; ignore non-item
            if key == "story_ttl" and not item.get("aweme_id") and not item.get("group_id"):
                continue
            return True
    for key, value in item.items():
        if key in ("author", "user", "music", "video", "statistics", "share_info", "text_extra"):
            continue
        key_text = str(key).lower()
        if ("story" in key_text or "moment" in key_text) and value not in (None, False, 0, "", [], {}):
            return True
    return False


def _filter_story_items(raw_items, sec_user_id, *, require_story_marker, require_author_match):
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        # Unwrap familiar/follow feed cards
        if "aweme" in item and isinstance(item.get("aweme"), dict) and not item.get("aweme_id"):
            item = item["aweme"]
        author = item_author_sec_uid(item)
        if require_author_match and author not in ("", sec_user_id):
            continue
        if require_story_marker and not is_time_limited_story(item):
            continue
        items.append(item)
    return items


ADB_SERIAL = os.environ.get("DOUYIN_ADB_SERIAL", "127.0.0.1:16384")
ADB_CANDIDATES = (
    Path(r"C:\Program Files\Netease\MuMuPlayer\nx_main\adb.exe"),
    Path(os.environ.get("LOCALAPPDATA") or "") / "Android" / "Sdk" / "platform-tools" / "adb.exe",
)


def _find_adb():
    for candidate in ADB_CANDIDATES:
        if candidate and candidate.is_file():
            return str(candidate)
    return ""


def _adb(args, timeout=20):
    adb = _find_adb()
    if not adb:
        raise RuntimeError("adb not found")
    return subprocess.run(
        [adb, "-s", ADB_SERIAL, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def _ui_nodes(xml_text):
    nodes = []
    for match in re.finditer(
        r'text="([^"]*)"[^>]*content-desc="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml_text or "",
    ):
        x1, y1, x2, y2 = (int(match.group(i)) for i in range(3, 7))
        nodes.append(
            {
                "text": match.group(1),
                "desc": match.group(2),
                "x": (x1 + x2) // 2,
                "y": (y1 + y2) // 2,
            }
        )
    return nodes


def _adb_tap_label(labels):
    dump = _adb(["shell", "uiautomator", "dump", "/sdcard/u_story.xml"])
    if dump.returncode != 0:
        return False
    pulled = _adb(["shell", "cat", "/sdcard/u_story.xml"])
    wanted = {label.lower() for label in labels}
    for node in _ui_nodes(pulled.stdout or ""):
        blob = f"{node['text']} {node['desc']}".lower()
        if any(label in blob for label in wanted):
            _adb(["shell", "input", "tap", str(node["x"]), str(node["y"])])
            return True
    return False


def _parse_share_command_blk(data):
    text = (data or b"").decode("utf-8", errors="ignore")
    match = re.search(r"https://v\.douyin\.com/[A-Za-z0-9_\-]+/?", text)
    return match.group(0) if match else ""


def _pull_newest_story_cache():
    listing = _adb(
        [
            "shell",
            "ls",
            "-t",
            "/data/data/com.ss.android.ugc.aweme/cache/cachev2",
        ]
    )
    if listing.returncode != 0:
        return ""
    newest = ""
    for line in (listing.stdout or "").splitlines():
        name = line.strip().split()[-1] if line.strip() else ""
        if name.endswith(".mdl") and "h264" in name:
            newest = name
            break
    if not newest:
        return ""
    dest = Path(os.environ.get("TEMP") or APP_DIR) / "aweme_story_cache.mp4"
    remote = f"/data/data/com.ss.android.ugc.aweme/cache/cachev2/{newest}"
    pulled = _adb(["pull", remote, str(dest)], timeout=60)
    if pulled.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        return ""
    return str(dest)


def fetch_stories_via_emulator(sec_user_id, user_id=""):
    """
    Last-resort harvest: open the profile 日常 on the MuMu app, copy the
    share link, and pull the cached media. Story25 is filtered from HTTP.
    """
    if not _find_adb():
        return None, "emulator_story: adb not found"
    numeric_uid = str(user_id or "").strip()
    try:
        devices = subprocess.run(
            [_find_adb(), "devices"],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return None, f"emulator_story: {exc}"
    if ADB_SERIAL not in (devices.stdout or ""):
        return None, "emulator_story: device offline"

    if numeric_uid.isdigit():
        deep = f"snssdk1128://user/profile/{numeric_uid}"
    else:
        deep = f"https://www.douyin.com/user/{sec_user_id}"
    _adb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", deep])
    time.sleep(2.5)
    if not _adb_tap_label(("日常", "最近 24")):
        return None, "emulator_story: 日常 tab not found"
    time.sleep(1.2)
    if not _adb_tap_label(("小时前", "图片", "视频")):
        # Cover may have no text; tap a typical first-cell region.
        _adb(["shell", "input", "tap", "160", "780"])
    time.sleep(2.0)
    share_blk = Path(os.environ.get("TEMP") or APP_DIR) / "share_command.blk"
    _adb(
        [
            "pull",
            "/data/data/com.ss.android.ugc.aweme/files/keva/repo/share_command/share_command.blk",
            str(share_blk),
        ]
    )
    share_url = ""
    if share_blk.is_file():
        share_url = _parse_share_command_blk(share_blk.read_bytes())
    if not share_url:
        _adb_tap_label(("分享",))
        time.sleep(0.8)
        _adb_tap_label(("分享链接", "复制链接"))
        time.sleep(0.8)
        _adb(
            [
                "pull",
                "/data/data/com.ss.android.ugc.aweme/files/keva/repo/share_command/share_command.blk",
                str(share_blk),
            ]
        )
        if share_blk.is_file():
            share_url = _parse_share_command_blk(share_blk.read_bytes())

    local = _pull_newest_story_cache()
    if not local:
        return None, "emulator_story: no cached media"
    aweme_id = ""
    if share_url:
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                aweme_id = resolve_share_link(client, share_url)
        except Exception:
            aweme_id = extract_aweme_id(share_url)
    aweme = {
        "aweme_id": aweme_id or Path(local).stem,
        "desc": "日常",
        "create_time": int(time.time()),
        "is_story": 1,
        "is_25_story": 1,
        "share_url": share_url,
        "author": {"sec_uid": sec_user_id, "uid": numeric_uid},
        "_local_media_path": local,
        "_source": "emulator_cachev2",
    }
    return [aweme], "emulator://story25"


def fetch_stories(client, profile, sec_user_id, user_id=""):
    last_message = ""
    supported = False
    supported_message = ""
    mobile_cookie = _mobile_cookie_header((profile.get("cookies") or "").strip())

    # 1) Mobile post API — only time-limited 日常 that also appear as posts.
    #    Regular 图文 (aweme_type=68 without story markers) stay in the
    #    profile feed forever and must not hide an active 24h ring.
    try:
        mobile_items, mobile_source = fetch_stories_via_mobile_post_api(
            client, sec_user_id, cookie_header=mobile_cookie,
        )
        if mobile_items:
            items = _filter_story_items(
                mobile_items,
                sec_user_id,
                require_story_marker=True,
                require_author_match=True,
            )
            if items:
                return items, mobile_source, True
        if mobile_source and "not available" not in mobile_source:
            last_message = mobile_source
    except LoginRequiredError:
        raise
    except Exception as exc:
        last_message = f"mobile_post_api: {exc}"

    # 2) Mobile story/feed — the actual 24h/日常 tray. Web cookies plus a
    #    signed request can still return a pack when insert_ids is set.
    try:
        feed_items, feed_source = fetch_stories_via_mobile_story_feed(
            client,
            sec_user_id,
            user_id=user_id,
            cookie_header=mobile_cookie,
        )
        if feed_items:
            items = _filter_story_items(
                feed_items,
                sec_user_id,
                require_story_marker=False,
                require_author_match=True,
            )
            if not items:
                items = [
                    item
                    for item in feed_items
                    if isinstance(item, dict)
                    and item_author_sec_uid(item) in ("", sec_user_id)
                ]
            if items:
                return items, feed_source, True
        if feed_source:
            last_message = feed_source
            if STORY_PROFILE_LIST_PATH in feed_source and "empty pack" in feed_source:
                return [], feed_source, True
            if "empty pack" in feed_source or feed_source.startswith("http"):
                supported = True
                supported_message = feed_source
    except LoginRequiredError:
        raise
    except Exception as exc:
        last_message = f"mobile_story_feed: {exc}"

    # 3) Signed mobile life/feed POST — this is the real 24h pack API.
    try:
        life_items, life_source = fetch_stories_via_mobile_life_feed(
            client,
            sec_user_id,
            user_id=user_id,
            cookie_header=mobile_cookie,
        )
        if life_items:
            items = _filter_story_items(
                life_items,
                sec_user_id,
                require_story_marker=False,
                require_author_match=True,
            )
            if not items:
                items = [
                    item
                    for item in life_items
                    if isinstance(item, dict)
                    and item_author_sec_uid(item) in ("", sec_user_id)
                ]
            if items:
                return items, life_source, True
        if life_source:
            last_message = life_source
            if "empty pack" in life_source or life_source.startswith("http"):
                supported = True
                supported_message = life_source
    except LoginRequiredError:
        raise
    except Exception as exc:
        last_message = f"mobile_life_feed: {exc}"

    # 4) Web-signed life/feed — accepted schema, often empty for 24h rings.
    try:
        life_data, life_source = request_life_feed(client, profile, sec_user_id, user_id=user_id)
    except LoginRequiredError:
        raise
    except Exception as exc:
        life_data, life_source = None, f"{LIFE_FEED_PATH}: {exc}"
    if life_data is not None:
        supported = True
        if response_has_login_tip(life_data):
            raise LoginRequiredError("Douyin is asking this session to log in before showing stories")
        raw_items = normalize_items(life_data)
        items = _filter_story_items(
            raw_items,
            sec_user_id,
            require_story_marker=False,
            require_author_match=True,
        )
        # life/feed items are stories by definition when nested under user_story_list
        if not items and raw_items:
            items = [
                item
                for item in raw_items
                if isinstance(item, dict)
                and item_author_sec_uid(item) in ("", sec_user_id)
            ]
        if items:
            return items, life_source if life_source.startswith("http") else LIFE_FEED_PATH, True
        message = life_source if "no active" in life_source else f"{LIFE_FEED_PATH}: no active visible stories"
        last_message = message
        supported_message = message

    # 5) Web candidates (familiar friend posts, legacy moment paths).
    for path in STORY_PATH_CANDIDATES:
        try:
            data = request_json(client, profile, path, sec_user_id, 0, 20, path_kind="story")
        except Exception as exc:
            last_message = f"{path}: {exc}"
            continue
        if response_has_login_tip(data):
            raise LoginRequiredError("Douyin is asking this session to log in before showing stories")
        if isinstance(data, dict):
            try:
                if int(data.get("status_code") or 0) == 0:
                    supported = True
            except (TypeError, ValueError):
                pass
        raw_items = normalize_items(data)
        if path == FAMILIAR_FEED_PATH:
            items = _filter_story_items(
                raw_items,
                sec_user_id,
                require_story_marker=True,
                require_author_match=True,
            )
        else:
            items = _filter_story_items(
                raw_items,
                sec_user_id,
                require_story_marker=False,
                require_author_match=True,
            )
        if items:
            return items, path, True
        if data:
            message = f"{path}: no active visible stories"
            last_message = message
            if supported and not supported_message:
                supported_message = message
            # Do NOT early-return on empty familiar feed — it only carries friend
            # posts that happen to be stories, and is often empty even when
            # life/feed would (or did) report pack status.

    ring_active = False
    try:
        ring_active = profile_has_active_story(client, profile, sec_user_id)
    except LoginRequiredError:
        raise
    except Exception:
        ring_active = False

    # 5) Logged-in browser: click the avatar ring and harvest the viewer APIs.
    if ring_active:
        try:
            browser_items, browser_source = fetch_stories_via_browser(profile, sec_user_id)
            if browser_items:
                items = _filter_story_items(
                    browser_items,
                    sec_user_id,
                    require_story_marker=False,
                    require_author_match=True,
                )
                if not items:
                    items = [
                        item
                        for item in browser_items
                        if isinstance(item, dict)
                        and item_author_sec_uid(item) in ("", sec_user_id)
                    ]
                if items:
                    return items, browser_source, True
            if browser_source:
                last_message = browser_source
        except LoginRequiredError:
            raise
        except Exception as exc:
            last_message = f"browser_story: {exc}"

    if ring_active:
        return [], MOBILE_ONLY_STORY_MESSAGE, False
    return [], supported_message or last_message or "No supported story endpoint returned items", supported


def result_to_dict(result):
    return {
        "status": result.status,
        "checked": result.checked,
        "downloaded": result.downloaded,
        "skipped": result.skipped,
        "failed": result.failed,
        "message": result.message,
        "files": result.files,
    }


def download_profile(
    profile,
    settings=None,
    *,
    videos=True,
    stories=False,
    limit=0,
    progress_callback=None,
):
    # Posted works use the app login (signed mobile post API) when a session
    # exists. Without login, public works still go through anonymous HTTP/browser.
    # Live-room probes stay cookieless and are not part of this path.
    profile = dict(profile)
    settings = settings or {}
    output_dir = Path(profile.get("output_dir") or ROOT_DOWNLOAD_DIR / safe_name(profile.get("name")))
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path, state = load_state(output_dir)
    summary = {
        "profile_id": profile.get("id"),
        "profile": profile.get("name"),
        "output_dir": str(output_dir),
        "videos": result_to_dict(MediaResult(status="disabled")),
        "stories": result_to_dict(MediaResult(status="disabled")),
    }
    proxy = profile.get("proxy_addr") or None
    report_progress(progress_callback, phase="resolving")
    # OPT-F: Skip async overhead when sec_uid is directly in the profile URL.
    _quick_sec = (
        extract_sec_uid_from_url(profile.get("original_profile_url", ""))
        or extract_sec_uid_from_url(profile.get("url", ""))
    )
    if _quick_sec:
        identity = {"sec_user_id": _quick_sec, "user_id": "", "nickname": profile.get("name") or ""}
    else:
        identity = httpx.run(resolve_profile_identity(profile)) if hasattr(httpx, "run") else None
        if identity is None:
            import asyncio

            identity = asyncio.run(resolve_profile_identity(profile))
    summary["sec_user_id"] = identity["sec_user_id"]
    summary["user_id"] = identity.get("user_id", "")
    summary["resolved_name"] = identity.get("nickname", "")
    timeout = httpx.Timeout(30, connect=10, read=20, write=20, pool=20)
    with httpx.Client(timeout=timeout, follow_redirects=True, http2=True, proxy=proxy) as client:
        if videos:
            try:
                report_progress(
                    progress_callback,
                    phase="scanning",
                    media_kind="video",
                    pages=0,
                    found=0,
                )
                # App-session path first: signed mobile post list sees notes,
                # restricted works, and time-limited stories. Fall back to the
                # anonymous web/browser paths when no session exists or mobile
                # does not return items.
                posts = None
                _fp_uid = identity["sec_user_id"]
                _app_cookie = _mobile_cookie_header((profile.get("cookies") or "").strip())
                if _app_cookie:
                    try:
                        posts = fetch_posts_via_mobile_api(
                            client,
                            _fp_uid,
                            limit=limit,
                            cookie_header=_app_cookie,
                        )
                        if posts:
                            logging.info(
                                "App login mobile post API returned %d posts for %s.",
                                len(posts),
                                profile.get("name"),
                            )
                    except LoginRequiredError:
                        posts = None
                    except Exception as _mob_exc:
                        logging.debug(
                            "App login mobile post API failed for %s: %s",
                            profile.get("name"),
                            _mob_exc,
                        )
                        posts = None

                # OPT-D: Skip HTTP if it has failed repeatedly for this profile.
                if not posts:
                    _fp_entry = _http_fastpath_failures.get(_fp_uid, (0, 0))
                    _fp_count, _fp_last_ts = _fp_entry if isinstance(_fp_entry, tuple) else (_fp_entry, 0)
                    # FIX-4.1: Auto-reset counter after cooldown so transient failures
                    # don't permanently disable the HTTP fast-path.
                    if _fp_count >= _HTTP_FASTPATH_MAX_FAILURES and (time.time() - _fp_last_ts) > _HTTP_FASTPATH_COOLDOWN:
                        with _http_fastpath_lock:  # FIX-7.1
                            _fp_count = 0
                            _http_fastpath_failures[_fp_uid] = (0, 0)
                        logging.info("HTTP fast-path cooldown expired for %s, re-enabling.", profile.get("name"))
                    _fp_skip = _fp_count >= _HTTP_FASTPATH_MAX_FAILURES
                    if _fp_skip:
                        logging.debug("HTTP fast-path skipped for %s (too many consecutive failures).", profile.get("name"))
                    else:
                        _fp_skip_counter = False
                        try:
                            posts = fetch_posts(
                                client, profile, _fp_uid,
                                limit=limit, progress_callback=progress_callback,
                            )
                            if posts:
                                _http_fastpath_failures[_fp_uid] = (0, 0)  # reset on success
                                logging.info(
                                    "HTTP fast-path returned %d posts for %s (no browser needed).",
                                    len(posts), profile.get("name"),
                                )
                        except CaptchaDetectedError:
                            raise  # captcha on HTTP ? propagate, do NOT try browser
                        except LoginRequiredError as _fp_exc:
                            # FIX-8.1: LoginRequired should fall through to browser (which
                            # applies session cookies), but NOT increment the failure counter
                            # since it's a permanent condition, not a transient HTTP issue.
                            logging.debug("HTTP fast-path got LoginRequired for %s; trying browser with cookies.", profile.get("name"))
                            posts = None
                            _fp_skip_counter = True  # signal to skip counter increment below
                        except EmptyApiResponseError as _fp_exc:
                            logging.debug("HTTP fast-path got %s for %s; falling through to browser.", type(_fp_exc).__name__, profile.get("name"))
                            posts = None  # expected - fall through to browser
                        except Exception as _fp_exc:
                            logging.debug("HTTP fast-path unexpected %s for %s; falling through to browser.", type(_fp_exc).__name__, profile.get("name"))
                            posts = None  # any other HTTP failure - browser fallback
                        if not posts and not _fp_skip_counter:
                            with _http_fastpath_lock:  # FIX-7.1
                                _fp_prev = _http_fastpath_failures.get(_fp_uid, (0, 0))
                                _fp_prev_count = _fp_prev[0] if isinstance(_fp_prev, tuple) else _fp_prev
                                _http_fastpath_failures[_fp_uid] = (_fp_prev_count + 1, time.time())

                if not posts:
                    try:
                        posts = fetch_posts_via_browser(
                            profile,
                            identity["sec_user_id"],
                            limit=limit,
                            progress_callback=progress_callback,
                        )
                    except (EmptyApiResponseError, Exception) as _browser_exc:
                        logging.debug(
                            "Browser fallback failed for %s (%s); trying mobile API.",
                            profile.get("name"), type(_browser_exc).__name__,
                        )
                        posts = None
                if posts is None:
                    # Last-resort mobile retry after anonymous web/browser failed.
                    try:
                        _mob_cookie = _mobile_cookie_header((profile.get("cookies") or "").strip())
                        posts = fetch_posts_via_mobile_api(
                            client,
                            identity["sec_user_id"],
                            limit=limit,
                            cookie_header=_mob_cookie,
                        )
                        if posts:
                            logging.info(
                                "Mobile API fallback returned %d posts for %s.",
                                len(posts), profile.get("name"),
                            )
                    except Exception as _mob_exc:
                        logging.debug(
                            "Mobile API fallback also failed for %s: %s",
                            profile.get("name"), _mob_exc,
                        )
                        raise
                result = download_aweme_items(
                    client,
                    profile,
                    posts,
                    output_dir,
                    state,
                    "video",
                    progress_callback=progress_callback,
                )
                result.status = "ok"
            except CaptchaDetectedError as exc:
                result = MediaResult(status="captcha", message=str(exc))
            except LoginRequiredError as exc:
                result = MediaResult(status="login_required", message=str(exc))
            except EmptyApiResponseError as exc:
                result = MediaResult(status="api_empty", message=str(exc))
            except Exception as exc:
                result = MediaResult(status="error", message=str(exc))
            summary["videos"] = result_to_dict(result)
        if stories:
            try:
                report_progress(progress_callback, phase="checking_stories", media_kind="story")
                story_items, story_source, story_supported = fetch_stories(
                    client,
                    profile,
                    identity["sec_user_id"],
                    user_id=identity.get("user_id") or "",
                )
                result = download_aweme_items(
                    client,
                    profile,
                    story_items,
                    output_dir,
                    state,
                    "story",
                    progress_callback=progress_callback,
                )
                if story_items:
                    result.status = "ok"
                elif story_source == MOBILE_ONLY_STORY_MESSAGE:
                    result.status = "mobile_only"
                elif story_supported:
                    result.status = "no_active_stories"
                else:
                    result.status = "unavailable"
                result.message = story_source
            except CaptchaDetectedError as exc:
                result = MediaResult(status="captcha", message=str(exc))
            except LoginRequiredError as exc:
                result = MediaResult(status="login_required", message=str(exc))
            except EmptyApiResponseError as exc:
                result = MediaResult(status="api_empty", message=str(exc))
            except Exception as exc:
                result = MediaResult(status="error", message=str(exc))
            summary["stories"] = result_to_dict(result)
    state["last_summary"] = summary
    save_state(state_path, state)
    return summary


# ---------------------------------------------------------------------------
# Single video download (share links / one-off video URLs)
# ---------------------------------------------------------------------------

AWEME_DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
DEFAULT_SINGLE_VIDEO_DIR_NAME = "Single Videos"


def _single_video_result(status, message, title="", author="", output_dir=""):
    return {
        "status": status,
        "message": message,
        "files": [],
        "title": title,
        "author": author,
        "output_dir": output_dir,
    }


def extract_url_from_text(text):
    """Pull the first http(s) URL out of pasted Douyin share text."""
    match = re.search(r"https?://[^\s\u4e00-\u9fff\"'<>]+", text or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;!?)]}\u3002\uff0c\uff09\uff01")


def extract_aweme_id(url):
    """Extract the numeric aweme id from a Douyin video/note URL."""
    if not url:
        return ""
    match = re.search(r"(?:/(?:video|note)/|modal_id=|share/(?:video|note)/)(\d{6,25})", url)
    return match.group(1) if match else ""


def resolve_share_link(client, url):
    """Follow v.douyin.com short-link redirects to find the target aweme id."""
    aweme_id = extract_aweme_id(url)
    if aweme_id:
        return aweme_id
    try:
        response = client.get(url, follow_redirects=True)
    except Exception:
        return ""
    candidates = [str(item.url) for item in response.history] + [str(response.url)]
    for candidate in reversed(candidates):
        aweme_id = extract_aweme_id(candidate)
        if aweme_id:
            return aweme_id
    # Some links land on a discovery/landing page that embeds the id in markup.
    try:
        body = response.text
    except Exception:
        body = ""
    for pattern in (
        r'"aweme_id"\s*:\s*"(\d{6,25})"',
        r"aweme_id=(\d{6,25})",
        r"/(?:video|note)/(\d{6,25})",
    ):
        match = re.search(pattern, body)
        if match:
            return match.group(1)
    return ""


def _detail_query(aweme_id, cookie_header):
    """Build signed-API query params for /aweme/v1/web/aweme/detail/."""
    params = default_query({"cookies": cookie_header or ""}, "", 0, 1, path_kind="post")
    for key in (
        "sec_user_id",
        "max_cursor",
        "locate_query",
        "show_live_replay_strategy",
        "need_time_list",
        "time_list_query",
        "whale_cut_token",
        "cut_version",
        "count",
        "publish_video_strategy_type",
        "from_user_page",
    ):
        params.pop(key, None)
    params["aweme_id"] = str(aweme_id)
    return params


def fetch_aweme_detail(client, aweme_id, cookie_header=""):
    """Fetch one aweme via the signed web detail API (videos and image notes)."""
    cookies = (cookie_header or "").strip() or _mobile_cookie_header()
    params = _detail_query(aweme_id, cookies)
    url, ua = signed_douyin_url(AWEME_DETAIL_PATH, params)
    headers = _request_headers({"cookies": cookies}, "", ua, cookies, params["msToken"])
    response = client.get(url, headers=headers)
    if response.status_code == 200 and not response.content:
        # One retry with a fresh signature, matching request_json behaviour.
        params = _detail_query(aweme_id, cookies)
        url, ua = signed_douyin_url(AWEME_DETAIL_PATH, params)
        headers = _request_headers({"cookies": cookies}, "", ua, cookies, params["msToken"])
        response = client.get(url, headers=headers)
    if not response.content:
        raise EmptyApiResponseError("Douyin returned an empty response for the video detail request")
    response.raise_for_status()
    data = _parse_json_response(response)
    if response_has_login_tip(data):
        raise LoginRequiredError("Douyin is asking this session to log in before viewing the video")
    aweme = data.get("aweme_detail") if isinstance(data, dict) else None
    if not isinstance(aweme, dict):
        try:
            status_code = int(data.get("status_code") or 0) if isinstance(data, dict) else -1
        except (TypeError, ValueError):
            status_code = -1
        raise RuntimeError(f"Douyin detail API returned no aweme_detail (status_code={status_code})")
    return aweme


def fetch_aweme_detail_via_share_page(client, aweme_id):
    """Fallback: scrape the public share page, which needs no request signing."""
    for kind in ("video", "note"):
        share_url = f"https://www.iesdouyin.com/share/{kind}/{aweme_id}/"
        try:
            response = client.get(share_url, headers={"User-Agent": USER_AGENT})
        except Exception:
            continue
        if response.status_code != 200:
            continue
        match = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", response.text, re.S)
        if not match:
            continue
        try:
            router = json.loads(match.group(1))
        except ValueError:
            continue
        loader = router.get("loaderData") if isinstance(router, dict) else None
        if not isinstance(loader, dict):
            continue
        for value in loader.values():
            if not isinstance(value, dict):
                continue
            info = value.get("videoInfoRes")
            items = info.get("item_list") if isinstance(info, dict) else None
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items[0]
    return None


def download_video_by_url(url, output_dir=None, progress_callback=None):
    """Download one Douyin work from a share link or video URL.

    Accepts pasted app share text, v.douyin.com short links, and full
    douyin.com video/note URLs.  Returns a dict with status
    ("ok"/"skipped"/"error"), message, files, title, author, output_dir.
    """
    link = extract_url_from_text((url or "").strip())
    if not link:
        return _single_video_result("error", "No Douyin link found in the pasted text.")
    out_dir = (
        Path(output_dir).expanduser()
        if str(output_dir or "").strip()
        else ROOT_DOWNLOAD_DIR / DEFAULT_SINGLE_VIDEO_DIR_NAME
    )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _single_video_result("error", f"Cannot create output folder: {exc}", output_dir=str(out_dir))
    cookies = _mobile_cookie_header()
    timeout = httpx.Timeout(60, connect=15, read=60, write=60, pool=30)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, http2=False) as client:
            aweme_id = extract_aweme_id(link) or resolve_share_link(client, link)
            if not aweme_id:
                return _single_video_result(
                    "error", f"Could not resolve a video id from {link}.", output_dir=str(out_dir)
                )
            aweme = None
            errors = []
            try:
                aweme = fetch_aweme_detail(client, aweme_id, cookies)
            except Exception as exc:
                errors.append(f"detail API {type(exc).__name__}: {exc}")
                logging.warning("aweme detail fetch failed for %s: %s", aweme_id, exc)
            if not isinstance(aweme, dict):
                try:
                    aweme = fetch_aweme_detail_via_share_page(client, aweme_id)
                except Exception as exc:
                    errors.append(f"share page {type(exc).__name__}: {exc}")
                if not isinstance(aweme, dict):
                    detail = "; ".join(errors) or "no aweme detail returned"
                    return _single_video_result(
                        "error", f"Could not fetch video details: {detail}", output_dir=str(out_dir)
                    )
            state_path, _existing_state = load_state(out_dir)
            fresh_state = {"downloaded_video_ids": [], "downloaded_story_ids": []}
            media_result = download_aweme_items(
                client, {"cookies": cookies}, [aweme], out_dir, fresh_state, "video", progress_callback
            )
            save_state(state_path, fresh_state)
    except Exception as exc:
        logging.exception("Single video download failed for %s", link)
        return _single_video_result("error", f"{type(exc).__name__}: {exc}", output_dir=str(out_dir))
    title = safe_name(aweme.get("desc") or (aweme.get("share_info") or {}).get("share_title") or aweme_id)
    author = (aweme.get("author") or {}).get("nickname") or ""
    if media_result.downloaded:
        return {
            "status": "ok",
            "message": f"Saved {media_result.downloaded} file(s) to {out_dir}",
            "files": media_result.files,
            "title": title,
            "author": author,
            "output_dir": str(out_dir),
        }
    if media_result.skipped:
        return _single_video_result(
            "skipped", "This video is already in the output folder.", title, author, str(out_dir)
        )
    return _single_video_result(
        "error", media_result.message or "Download failed; check logs for details.", title, author, str(out_dir)
    )



def load_profiles():
    profiles = load_json(PROFILES_FILE, [])
    return profiles if isinstance(profiles, list) else []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download Douyin profile videos and stories.")
    parser.add_argument("--profile-id", help="Profile id from profiles.json")
    parser.add_argument("--profile-url", help="One-off Douyin user URL")
    parser.add_argument("--output-dir", help="Output directory for one-off URL")
    parser.add_argument("--videos", action="store_true", help="Download profile works/videos")
    parser.add_argument("--stories", action="store_true", help="Download time-limited stories")
    parser.add_argument("--video-url", help="One-off download of a single Douyin video (share link or video URL)")
    parser.add_argument("--limit", type=int, default=0, help="Maximum works to download; 0 means all fetched")
    parser.add_argument(
        "--import-chrome-login",
        action="store_true",
        help="Import the logged-in Douyin session from Chrome before downloading",
    )
    parser.add_argument(
        "--chrome-cdp",
        default=DEFAULT_CHROME_CDP,
        help=f"Chrome debugging address (default: {DEFAULT_CHROME_CDP})",
    )
    args = parser.parse_args(argv)
    videos = args.videos or not args.stories
    if args.video_url:
        if args.import_chrome_login:
            import_chrome_session(args.chrome_cdp)
        result = download_video_by_url(args.video_url, args.output_dir)
        print_console(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in ("ok", "skipped") else 1
    profiles = load_profiles()
    if args.profile_id:
        profile = next((item for item in profiles if item.get("id") == args.profile_id), None)
        if not profile:
            raise SystemExit(f"Profile not found: {args.profile_id}")
    elif args.profile_url:
        profile = {
            "id": "one-off",
            "name": "Douyin profile",
            "url": args.profile_url,
            "original_profile_url": args.profile_url,
            "output_dir": args.output_dir or str(ROOT_DOWNLOAD_DIR / "Douyin profile"),
            "cookies": "",
            "proxy_addr": "",
            "stream_orientation": 1,
        }
    else:
        raise SystemExit("Use --profile-id or --profile-url")
    if args.import_chrome_login:
        import_chrome_session(args.chrome_cdp)
    settings = load_json(SETTINGS_FILE, {})
    summary = download_profile(profile, settings, videos=videos, stories=args.stories, limit=args.limit)
    print_console(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
