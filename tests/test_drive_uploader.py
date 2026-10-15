"""Offline fault injection for the upload/verify/journal/delete transaction."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "drive_uploader", Path(__file__).resolve().parents[1] / "uploader" / "drive_uploader.py"
)
uploader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uploader)
ITEM_ID = "7696544512231551040"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Network access is forbidden in uploader tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    status = tmp_path / "src" / "status.json"
    status.parent.mkdir()
    status.write_text('{"engines": {}, "profiles": []}')
    journal = tmp_path / "uploaded.jsonl"
    for name, value in {"ROOT": root, "STATUS": status, "LOG": journal,
                        "LOCK": tmp_path / "uploader.lock", "REMOTE": "fixture:Douyin"}.items():
        monkeypatch.setattr(uploader, name, value)

    def make_file(rel=f"acct/stories/stamp_{ITEM_ID}_caption.mp4", data=b"complete capture"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        old = uploader.time.time() - uploader.MIN_AGE - 60
        os.utime(path, (old, old))
        return path

    return SimpleNamespace(root=root, status=status, journal=journal, make_file=make_file)


class FakeDrive:
    """Only these fake rclone commands can run; bytes stand in for Drive objects."""
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.hook = lambda _stage: None
        self.copy_rc = self.check_rc = self.md5_rc = 0
        self.partial = False
        self.bad_md5 = False
        self.opened = set()
        self.lsof_rc = 0
        self.lsof_stderr = ""

    def run(self, args, **_kwargs):
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, self.lsof_rc,
                "".join(f"n{p}\n" for p in self.opened), self.lsof_stderr)
        assert args[0] == "rclone", args
        stage = args[1]
        self.calls.append(args)
        self.hook(stage)
        rc, out = 0, ""
        if stage == "copyto":
            data = Path(args[-2]).read_bytes()
            self.objects[args[-1]] = data[:3] if self.partial else data
            rc = self.copy_rc
        elif stage == "check":
            listing = Path(args[args.index("--files-from-raw") + 1])
            names = listing.read_text().splitlines()
            assert len(names) == 1
            source = Path(args[-2]) / names[0]
            remote = f"{args[-1]}/{names[0]}"
            rc = self.check_rc or int(self.objects.get(remote) != source.read_bytes())
        elif stage == "md5sum":
            rc = self.md5_rc
            data = self.objects.get(args[-1])
            if data is not None:
                digest = "0" * 32 if self.bad_md5 else hashlib.md5(data).hexdigest()
                out = f"{digest}  {args[-1]}\n"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, rc, out, "synthetic Drive failure" if rc else "")


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(uploader.subprocess, "run", fake.run)
    return fake


def upload(path, sandbox, dry=False):
    return uploader.upload_one(path, path.relative_to(sandbox.root), path.stat().st_size, dry)


def test_verified_upload_fsyncs_receipt_before_unlink(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    data = path.read_bytes()
    original_fsync = uploader.os.fsync
    synced = []

    def fsync(fd):
        assert path.exists()
        row = json.loads(sandbox.journal.read_text())
        assert row["md5"] == hashlib.md5(data).hexdigest()
        assert drive.objects[row["drive_path"]] == data
        synced.append(True)
        original_fsync(fd)

    monkeypatch.setattr(uploader.os, "fsync", fsync)
    assert upload(path, sandbox) is True
    assert synced and not path.exists()
    row = json.loads(sandbox.journal.read_text())
    assert row["item_id"] == ITEM_ID and row["kind"] == "stories"
    assert row["size"] == len(data)


@pytest.mark.parametrize("failure", ["copy", "partial-copy", "check", "md5-command", "md5-mismatch", "missing-remote"])
def test_drive_errors_keep_local_and_do_not_journal(sandbox, drive, failure):
    path = sandbox.make_file()
    if failure == "copy":
        drive.copy_rc = 7
    elif failure == "partial-copy":
        drive.partial = True
    elif failure == "check":
        drive.check_rc = 3
    elif failure == "md5-command":
        drive.md5_rc = 5
    elif failure == "md5-mismatch":
        drive.bad_md5 = True
    elif failure == "missing-remote":
        drive.hook = lambda stage: drive.objects.clear() if stage == "md5sum" else None
    assert upload(path, sandbox) is False
    assert path.read_bytes() == b"complete capture"
    assert not sandbox.journal.exists()


@pytest.mark.parametrize("stage", ["copyto", "check", "md5sum"])
def test_crash_mid_upload_can_retry(sandbox, drive, stage):
    path = sandbox.make_file()
    def crash(current):
        if current == stage:
            if current == "copyto":
                drive.objects["fixture:Douyin/acct/stories/" + path.name] = b"partial"
            raise KeyboardInterrupt("simulated process death")
    drive.hook = crash
    with pytest.raises(KeyboardInterrupt):
        upload(path, sandbox)
    assert path.exists() and not sandbox.journal.exists()
    drive.hook = lambda _stage: None
    assert upload(path, sandbox) is True
    assert not path.exists()


def test_crash_after_durable_receipt_before_delete_retries_safely(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    unlink = Path.unlink
    def crash(self, *args, **kwargs):
        if self == path:
            raise KeyboardInterrupt("simulated process death before delete")
        return unlink(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", crash)
        with pytest.raises(KeyboardInterrupt):
            upload(path, sandbox)
    assert path.exists()
    receipt = json.loads(sandbox.journal.read_text())
    assert drive.objects[receipt["drive_path"]] == path.read_bytes()
    assert upload(path, sandbox) is True
    assert not path.exists()


@pytest.mark.parametrize("stage", ["copyto", "check", "md5sum"])
@pytest.mark.parametrize("error", [FileNotFoundError("missing rclone"), subprocess.TimeoutExpired("rclone", 1)])
def test_command_exceptions_preserve_local(sandbox, drive, stage, error):
    path = sandbox.make_file()
    def fail(current):
        if current == stage:
            raise error
    drive.hook = fail
    assert upload(path, sandbox) is False
    assert path.exists() and not sandbox.journal.exists()


@pytest.mark.parametrize("change", ["grow", "rewrite", "replace", "open"])
def test_changes_during_verification_prevent_delete_and_receipt(sandbox, drive, change):
    path = sandbox.make_file()
    def mutate(stage):
        if stage != "md5sum":
            return
        if change == "grow":
            with path.open("ab") as f:
                f.write(b"more")
        elif change == "rewrite":
            old = path.stat()
            path.write_bytes(b"x" * old.st_size)
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        elif change == "replace":
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        else:
            drive.opened.add(str(path))
    drive.hook = mutate
    assert upload(path, sandbox) is False
    assert path.exists() and not sandbox.journal.exists()


def test_file_changes_after_scan_are_rechecked_before_copy(sandbox, drive):
    path = sandbox.make_file()
    [(path, rel, size)] = uploader.candidates()
    path.write_bytes(b"x" * size)
    assert uploader.upload_one(path, rel, size, False) is False
    assert not drive.calls and path.exists()


def test_file_changes_while_hashing_are_never_copied(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    original = uploader.md5
    def mutate(path):
        digest = original(path)
        path.write_bytes(b"changed while hashing")
        return digest
    monkeypatch.setattr(uploader, "md5", mutate)
    assert upload(path, sandbox) is False
    assert path.exists() and not drive.calls and not sandbox.journal.exists()


def test_finalizer_starting_during_upload_prevents_delete(sandbox, drive):
    path = sandbox.make_file("acct/live.mkv")
    def start(stage):
        if stage == "md5sum":
            sandbox.status.write_text('{"engines":{"active_finalizers":1}}')
    drive.hook = start
    assert upload(path, sandbox) is False
    assert path.exists() and not sandbox.journal.exists()


def test_file_growing_during_journal_fsync_is_not_deleted(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    fsync = uploader.os.fsync
    def grow(fd):
        fsync(fd)
        with path.open("ab") as handle:
            handle.write(b"new capture bytes")
    monkeypatch.setattr(uploader.os, "fsync", grow)
    assert upload(path, sandbox) is False
    assert path.read_bytes().endswith(b"new capture bytes")
    row = json.loads(sandbox.journal.read_text())
    assert drive.objects[row["drive_path"]] == b"complete capture"


@pytest.mark.parametrize("tail", [b'{"item_id": "torn', b'{"item_id": "old"}', b'\xfftorn'])
def test_append_after_torn_jsonl_keeps_new_receipt_readable(sandbox, drive, tail):
    sandbox.journal.write_bytes(b'{"item_id": "older"}\n' + tail)
    path = sandbox.make_file()
    assert upload(path, sandbox) is True
    row = json.loads(sandbox.journal.read_bytes().splitlines()[-1])
    assert row["item_id"] == ITEM_ID


def test_journal_fsync_failure_keeps_local(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    def fail(_fd):
        raise OSError("disk full")
    monkeypatch.setattr(uploader.os, "fsync", fail)
    assert upload(path, sandbox) is False
    assert path.exists()


@pytest.mark.parametrize("rel", ["acct/stories/x.mp4.part.123.abc", "acct/stories/x.mp4.tmp.abc",
    "acct/x.finalizing.mkv", "acct/.recording_sessions/s/part-0001.mkv", "acct/.hidden.mp4",
    "acct/stories/x.partial", "acct/douyin_media_state.json", "acct/x.log", "loose.mp4"])
def test_partial_and_internal_files_are_never_candidates(sandbox, drive, rel):
    sandbox.make_file(rel)
    assert uploader.candidates() == []


def test_recent_empty_open_and_symlink_files_are_skipped(sandbox, drive):
    recent = sandbox.make_file("acct/stories/recent.mp4")
    os.utime(recent, None)
    sandbox.make_file("acct/stories/empty.mp4", b"")
    opened = sandbox.make_file("acct/stories/open.mp4")
    drive.opened.add(str(opened))
    (sandbox.root / "acct" / "alias.mp4").symlink_to(opened)
    assert uploader.candidates() == []


@pytest.mark.parametrize("payload", [None, [], {"profiles": [None]}, {"profiles": [{"state": []}]},
    {"engines": []}, {"engines": {"active_finalizers": 1}, "profiles": [{"name": "other", "state": {"recording": True}}]}])
def test_unknown_or_active_finalizer_status_blocks_live_upload(sandbox, drive, payload):
    live = sandbox.make_file("acct/live.mkv")
    story = sandbox.make_file()
    sandbox.status.write_text(json.dumps(payload))
    assert [p for p, _rel, _size in uploader.candidates()] == [story]
    assert live.exists()


@pytest.mark.parametrize("status", ["missing", "invalid", "auxiliary", "recording", "deferred"])
def test_busy_or_unreadable_status_preserves_live_files(sandbox, drive, status):
    sandbox.make_file("acct/live.mkv")
    if status == "missing":
        sandbox.status.unlink()
    elif status == "invalid":
        sandbox.status.write_text("{torn")
    elif status == "auxiliary":
        sandbox.status.with_name("status.fixture.json").write_text('{"engines":{"active_finalizers":1}}')
    elif status == "recording":
        sandbox.status.write_text('{"profiles":[{"name":"acct","state":{"recording":true}}]}')
    else:
        sandbox.status.write_text('{"engines":{"deferred_finalizers":1}}')
    assert uploader.candidates() == []


@pytest.mark.parametrize("rc,stderr", [(2, "failure"), (1, "permission denied"), (0, "incomplete scan")])
def test_lsof_errors_fail_closed(sandbox, drive, rc, stderr):
    sandbox.make_file()
    drive.lsof_rc, drive.lsof_stderr = rc, stderr
    with pytest.raises(RuntimeError, match="lsof"):
        uploader.candidates()


def test_lsof_no_matches_is_safe(sandbox, drive):
    path = sandbox.make_file()
    drive.lsof_rc = 1
    assert [p for p, _rel, _size in uploader.candidates()] == [path]


@pytest.mark.skipif(not shutil.which("lsof"), reason="lsof unavailable")
@pytest.mark.parametrize("rel", ["acct/stories/video.mp4", "主播/stories/风景.mp4"])
def test_real_lsof_detects_held_open_file(sandbox, rel):
    path = sandbox.make_file(rel)
    with path.open("ab"):
        assert uploader.candidates() == []
    assert [p for p, _rel, _size in uploader.candidates()] == [path]


def test_dry_run_never_copies_journals_or_deletes(sandbox, drive):
    path = sandbox.make_file()
    assert upload(path, sandbox, dry=True) is None
    assert path.exists() and not drive.calls and not sandbox.journal.exists()


def test_live_destination_adds_lives_directory(sandbox, drive):
    path = sandbox.make_file("acct/live.mkv")
    assert upload(path, sandbox) is True
    assert json.loads(sandbox.journal.read_text())["drive_path"] == "fixture:Douyin/acct/lives/live.mkv"


def test_grouped_item_uses_parent_id_in_receipt(sandbox, drive):
    path = sandbox.make_file(f"acct/stories/{ITEM_ID}/stamp_caption.mp4")
    assert upload(path, sandbox) is True
    assert json.loads(sandbox.journal.read_text())["item_id"] == ITEM_ID


def test_main_continues_after_one_file_fails_and_returns_failure(sandbox, drive, monkeypatch):
    first = sandbox.make_file("acct/stories/a.mp4")
    second = sandbox.make_file("acct/stories/b.mp4")
    def fail_first(stage):
        if stage == "copyto":
            drive.copy_rc = 7 if drive.calls[-1][-2] == str(first) else 0
    drive.hook = fail_first
    monkeypatch.setattr(uploader.sys, "argv", ["drive_uploader.py"])
    assert uploader.main() == 1
    assert first.exists() and not second.exists()


def test_main_lock_prevents_overlapping_upload(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    monkeypatch.setattr(uploader.sys, "argv", ["drive_uploader.py"])
    with uploader.LOCK.open("w") as lock:
        uploader.fcntl.flock(lock, uploader.fcntl.LOCK_EX | uploader.fcntl.LOCK_NB)
        assert uploader.main() == 0
    assert path.exists() and not drive.calls


def test_main_reports_lsof_failure_without_uploading(sandbox, drive, monkeypatch):
    path = sandbox.make_file()
    drive.lsof_rc = 2
    monkeypatch.setattr(uploader.sys, "argv", ["drive_uploader.py"])
    assert uploader.main() == 1
    assert path.exists() and not drive.calls
