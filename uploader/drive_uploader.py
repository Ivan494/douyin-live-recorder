#!/usr/bin/env python3
"""Offload finished Douyin captures to Google Drive (rclone remote saridq:).

Only finished files: mtime stable >= MIN_AGE, not open by any process (lsof),
and no live-video upload for a profile that status.json shows recording/finalizing.
Each file: rclone copyto -> rclone check + independent md5 -> fsync receipt
-> delete unchanged local file. A crash can leave both copies, never neither.
flock prevents overlapping runs.
"""
import argparse, fcntl, hashlib, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ROOT = REPO / "downloads"
STATUS = REPO / "src" / "status.json"
LOG = HERE / "uploaded.jsonl"
LOCK = HERE / "uploader.lock"
REMOTE = os.environ.get("DOUYIN_DRIVE_REMOTE", "saridq:Douyin")
MIN_AGE = 300
SKIP_NAMES = {"douyin_media_state.json"}
SKIP_SUFFIX = (".log", ".part", ".tmp", ".partial", ".download", ".finalizing.mkv", ".lock")
STAGING_RE = re.compile(r"\.(?:part|tmp|partial|download)(?:\.|$)", re.IGNORECASE)
VIDEO = {".mkv", ".mp4", ".flv", ".ts"}
ID_RE = re.compile(r"_(\d{15,21})(?:_|\.|$)")


def log(msg):
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z} {msg}", flush=True)


def open_files():
    r = subprocess.run(["lsof", "-Fn", "+D", str(ROOT)], capture_output=True, text=True)
    # lsof returns 1 for no matches, but warnings mean its scan is incomplete.
    if r.returncode not in (0, 1) or r.stderr.strip():
        raise RuntimeError(f"lsof could not establish closed files: {r.stderr.strip()[-300:]}")
    return {l[1:] for l in r.stdout.splitlines() if l.startswith("n")}


def busy_profiles():
    """Profile folder names (anchor/name) with active recording; '*' if unknown."""
    busy = set()
    # src/status.json (service) plus src/status.*.json (test harnesses).
    for status_path in [STATUS, *sorted(STATUS.parent.glob("status.*.json"))]:
        try:
            d = json.loads(status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if status_path == STATUS:
                return {"*"}
            continue
        except Exception:
            return {"*"}
        if not isinstance(d, dict):
            return {"*"}
        eng = d.get("engines", {})
        profiles = d.get("profiles", [])
        if not isinstance(eng, dict) or not isinstance(profiles, list):
            return {"*"}
        # Finalizer counters are global; recording profiles cannot identify them.
        if eng.get("active_finalizers") or eng.get("deferred_finalizers"):
            return {"*"}
        found = set()
        for p in profiles:
            if not isinstance(p, dict):
                return {"*"}
            state = p.get("state", {})
            if not isinstance(state, dict):
                return {"*"}
            if state.get("recording"):
                names = [name for name in (p.get("name"), state.get("anchor_name")) if isinstance(name, str) and name]
                if not names:
                    return {"*"}
                found.update(names)
        if eng.get("active_recordings") and not found:
            found.add("*")
        busy |= found
    return busy


def category(rel_parts):
    # rel_parts: (account, ..., filename)
    if len(rel_parts) == 2:
        return "lives"  # recordings sit at the profile root
    return None  # keep sub-folder (stories/, videos/, images/, sessions) as-is


def drive_rel(rel):
    parts = rel.parts
    cat = category(parts)
    return Path(parts[0], cat, *parts[1:]) if cat else rel


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_stamp(path):
    """Detect replacement and same-size edits as well as growth."""
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def is_busy_live(path, rel, busy):
    return (path.suffix.lower() in VIDEO and "stories" not in rel.parts
            and ("*" in busy or rel.parts[0] in busy))


def candidates():
    now = time.time()
    opened = open_files()
    busy = busy_profiles()
    out = []
    for path in sorted(ROOT.rglob("*")):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(ROOT)
            if len(rel.parts) < 2 or any(p.startswith(".") for p in rel.parts):
                continue
            name = path.name
            if name in SKIP_NAMES or name.lower().endswith(SKIP_SUFFIX) or STAGING_RE.search(name):
                continue
            st = path.stat()
            if st.st_size == 0 or now - st.st_mtime < MIN_AGE or str(path) in opened:
                continue
            if is_busy_live(path, rel, busy):
                continue
            out.append((path, rel, st.st_size))
        except OSError:
            # Recorder cleanup/rename can race a directory scan.
            continue
    return out


def upload_one(path, rel, size, dry):
    try:
        return _upload_one(path, rel, size, dry)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log(f"FAIL {rel}: {exc}")
        return False


def _upload_one(path, rel, size, dry):
    drel = drive_rel(rel)
    dest = f"{REMOTE}/{drel.as_posix()}"
    if dry:
        log(f"DRY would upload {rel} ({size} B) -> {dest}")
        return None
    stamp = file_stamp(path)
    if (path.is_symlink() or not path.is_file() or stamp[2] != size or size <= 0
            or time.time() - path.stat().st_mtime < MIN_AGE
            or str(path) in open_files() or is_busy_live(path, rel, busy_profiles())):
        log(f"SKIP {rel}: no longer eligible")
        return False
    digest = md5(path)
    if file_stamp(path) != stamp:
        log(f"SKIP {rel}: changed while hashing")
        return False
    r = subprocess.run(["rclone", "copyto", "--retries", "3", str(path), dest], capture_output=True, text=True)
    if r.returncode:
        log(f"FAIL copy {rel}: {r.stderr.strip()[-300:]}")
        return False
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        tf.write(path.name + "\n")
    try:
        c = subprocess.run(["rclone", "check", "--one-way", "--files-from-raw", tf.name,
                            str(path.parent), f"{REMOTE}/{drel.parent.as_posix()}"],
                           capture_output=True, text=True)
    finally:
        os.unlink(tf.name)
    # independent md5 confirmation from Drive
    h = subprocess.run(["rclone", "md5sum", dest], capture_output=True, text=True)
    remote_md5 = h.stdout.split()[0] if h.returncode == 0 and h.stdout.strip() else ""
    if c.returncode or remote_md5 != digest:
        log(f"FAIL verify {rel}: check rc={c.returncode} remote_md5={remote_md5} local={digest}")
        return False
    if (path.is_symlink() or file_stamp(path) != stamp or str(path) in open_files()
            or is_busy_live(path, rel, busy_profiles())):
        log(f"SKIP delete {rel}: changed or busy during upload")
        return False
    m = ID_RE.search(path.name)
    item_id = m.group(1) if m else next(
        (part for part in reversed(rel.parts[2:-1]) if re.fullmatch(r"\d{15,21}", part)), None
    )
    entry = {"account": rel.parts[0], "item_id": item_id,
             "kind": drel.parts[1] if len(drel.parts) > 2 else "",
             "path": str(path), "size": size, "md5": digest, "drive_path": dest,
             "uploaded_at": datetime.now(timezone.utc).isoformat()}
    with open(LOG, "a+b") as f:
        # A killed writer may leave an unterminated record. Preserve it, while
        # ensuring the next durable receipt starts on its own readable line.
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    if (path.is_symlink() or file_stamp(path) != stamp or str(path) in open_files()
            or is_busy_live(path, rel, busy_profiles())):
        log(f"SKIP delete {rel}: changed or opened while journaling")
        return False
    path.unlink()
    log(f"OK {rel} -> {dest} ({size} B md5 {digest}) local removed")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", help="substring filter on relative path")
    a = ap.parse_args()
    with open(LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another run holds the lock; exit")
            return 0
        return run_uploads(a)


def run_uploads(a):
    try:
        items = candidates()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log(f"FAIL scan: {exc}")
        return 1
    if a.only:
        items = [i for i in items if a.only in str(i[1])]
    if a.limit:
        items = items[: a.limit]
    log(f"{len(items)} finished file(s) eligible{' (dry-run)' if a.dry_run else ''}")
    fails = 0
    for path, rel, size in items:
        if upload_one(path, rel, size, a.dry_run) is False:
            fails += 1
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
