"""Owner-only Linux session storage. Cookie values never enter diagnostic output."""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import uuid


SESSION_FORMAT = "linux-private-v1"


class PrivateSessionError(RuntimeError):
    """A session file could not be handled safely."""


@contextmanager
def _private_directory(path, *, create=False):
    if os.name == "nt":
        raise PrivateSessionError("Private Linux sessions require Linux.")
    parent = Path(path).parent
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise PrivateSessionError("Session directory has an unsafe owner or type.")
        if create:
            os.fchmod(descriptor, 0o700)
        elif stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PrivateSessionError("Session directory must be owner-only.")
        yield descriptor
    finally:
        os.close(descriptor)


def _check_existing_file(directory, name):
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise PrivateSessionError("Session destination has an unsafe owner or type.")


def validate_private_destinations(paths):
    """Reject unsafe destinations before starting a multi-file session import."""
    for path in paths:
        path = Path(path)
        with _private_directory(path, create=True) as directory:
            _check_existing_file(directory, path.name)


def save_private_json(path, data):
    """Replace a file atomically using a private, exclusive temporary file."""
    path = Path(path)
    with _private_directory(path, create=True) as directory:
        _check_existing_file(directory, path.name)
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        created = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            created = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # A changed destination must still be a regular file, never a link.
            _check_existing_file(directory, path.name)
            os.replace(
                temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory
            )
            os.fsync(directory)
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass


def load_private_json(path):
    """Read only an owner-owned regular file with no group/world permissions."""
    path = Path(path)
    with _private_directory(path) as directory:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise PrivateSessionError("Session file must be an owner-only regular file.")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                return json.load(stream)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def validate_cookie_header(value):
    if not isinstance(value, str) or any(character in value for character in "\r\n\0"):
        raise PrivateSessionError("Invalid session header.")
    return value.strip()


def save_cookie_header(path, cookie_header, *, source="stdin-import"):
    save_private_json(
        path,
        {
            "format": SESSION_FORMAT,
            "cookie_header": validate_cookie_header(cookie_header),
            "source": source,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def load_cookie_header(path):
    # Platform formats are deliberately separate; DPAPI is never treated as text.
    if os.name == "nt":
        return ""
    try:
        payload = load_private_json(path)
        if not isinstance(payload, dict) or payload.get("format") != SESSION_FORMAT:
            return ""
        return validate_cookie_header(payload.get("cookie_header"))
    except InterruptedError:
        raise
    except (OSError, ValueError, UnicodeError, PrivateSessionError):
        # JSON parser diagnostics can contain the credential being parsed.
        return ""
