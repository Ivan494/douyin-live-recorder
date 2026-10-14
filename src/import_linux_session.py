"""Import an authorized session and its bound mobile device from JSON on stdin.

Run: python src/import_linux_session.py < private-session-export.json
Input keys: web_cookie_header, mobile_cookie_header, mobile_device.
The output contains readiness booleans only; credentials are never echoed.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from linux_session_storage import (
    PrivateSessionError,
    load_cookie_header,
    save_cookie_header,
    save_private_json,
    validate_cookie_header,
    validate_private_destinations,
)


MAX_INPUT_SIZE = 512 * 1024
_LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "uid_tt", "uid_tt_ss"}
_DEVICE_FIELDS = {
    "device_id", "install_id", "cdid", "version_code", "version_name",
    "device_type", "device_brand", "os_version", "os_api", "own_uid",
    "channel", "update_version_code",
}


def _has_login(header):
    return bool(
        {
            part.split("=", 1)[0].strip()
            for part in header.split(";")
            if "=" in part and part.split("=", 1)[1].strip()
        }
        & _LOGIN_COOKIE_NAMES
    )


def _bound_device(value):
    if not isinstance(value, dict):
        raise PrivateSessionError("A bound mobile device is required.")
    device = {}
    for key in _DEVICE_FIELDS:
        if key not in value:
            continue
        field = value[key]
        if isinstance(field, bool) or not isinstance(field, (str, int)):
            raise PrivateSessionError("Invalid mobile device field.")
        field = str(field).strip()
        if len(field) > 512 or any(character in field for character in "\r\n\0"):
            raise PrivateSessionError("Invalid mobile device field.")
        device[key] = field
    for name in ("device_id", "install_id"):
        field = device.get(name, "")
        if not field.isdigit() or not 16 <= len(field) <= 24:
            raise PrivateSessionError("Valid bound device identifiers are required.")
    device["cdid"] = device.get("cdid") or str(uuid.uuid4())
    return device


def import_session(payload, directory):
    """Validate the complete input before replacing any existing session files."""
    if os.name == "nt" or not isinstance(payload, dict):
        raise PrivateSessionError("Linux session import is unavailable.")
    web_header = validate_cookie_header(payload.get("web_cookie_header", ""))
    mobile_header = validate_cookie_header(payload.get("mobile_cookie_header", ""))
    web_header = web_header or mobile_header
    mobile_header = mobile_header or web_header
    if not _has_login(web_header) or not _has_login(mobile_header):
        raise PrivateSessionError("An authenticated session is required.")
    device = _bound_device(payload.get("mobile_device"))
    directory = Path(directory)
    validate_private_destinations(
        directory / name
        for name in ("mobile_device.json", "douyin_session.json", "mobile_session.json")
    )
    # Bind the imported app cookie to the original device before writing headers.
    save_private_json(directory / "mobile_device.json", device)
    save_cookie_header(directory / "douyin_session.json", web_header)
    save_cookie_header(directory / "mobile_session.json", mobile_header)
    return {
        "web_ready": _has_login(load_cookie_header(directory / "douyin_session.json")),
        "mobile_ready": _has_login(load_cookie_header(directory / "mobile_session.json")),
        "device_ready": True,
    }


def main(argv=None, *, input_stream=None, output_stream=None):
    parser = argparse.ArgumentParser(description="Import a Linux Douyin session from stdin.")
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args(argv)
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    readiness = {"web_ready": False, "mobile_ready": False, "device_ready": False}
    code = 1
    try:
        raw = input_stream.read(MAX_INPUT_SIZE + 1)
        if len(raw) > MAX_INPUT_SIZE:
            raise PrivateSessionError("Session import exceeds the size limit.")
        readiness = import_session(json.loads(raw), arguments.directory)
        code = 0 if all(readiness.values()) else 1
    except InterruptedError:
        raise
    except (OSError, ValueError, UnicodeError, PrivateSessionError):
        # Keep even malformed input and filesystem exception details off stdout/stderr.
        pass
    output_stream.write(json.dumps(readiness, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
