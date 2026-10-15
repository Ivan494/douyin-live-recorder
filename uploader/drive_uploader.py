#!/usr/bin/env python3
"""Offload finished Douyin captures to Google Drive (rclone remote saridq:).

Only finished files: mtime stable >= MIN_AGE, not open by any process (lsof),
and no live-video upload for a profile that status.json shows recording/finalizing.
Each file: rclone copyto -> rclone check --one-way (size + md5) -> delete local
-> append uploaded.jsonl. flock prevents overlapping runs.
"""
import argparse, fcntl, hashlib, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/ubuntu/douyin-live-recorder")
ROOT = REPO / "downloads"
STATUS = REPO / "src" / "status.json"
HERE = Path(__file__).resolve().parent
LOG = HERE / "uploaded.jsonl"
LOCK = HERE / "uploader.lock"
REMOTE = os.environ.get("DOUYIN_DRIVE_REMOTE", "saridq:Douyin")
MIN_AGE = 300
SKIP_NAMES = {"douyin_media_state.json"}
SKIP_SUFFIX = (".log", ".part", ".tmp", ".partial", ".download", ".finalizing.mkv", ".lock")
VIDEO = {".mkv", ".mp4", ".flv", ".ts"}
ID_RE = re.compile(r"_(\d{15,21})(?:_|\.|$)")


def log(msg):
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z} {msg}", flush=True)


def open_files():
    r = subprocess.run(["lsof", "-Fn", "+D", str(ROOT)], capture_output=True, text=True)
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
        eng = d.get("engines", {})
        found = set()
        for p in d.get("profiles", []):
            state = p.get("state") or {}
            if state.get("recording"):
                found.update(filter(None, [p.get("name"), state.get("anchor_name")]))
        if (eng.get("active_recordings") or eng.get("active_finalizers") or eng.get("deferred_finalizers")) and not found:
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


def candidates():
    now = time.time()
    opened = open_files()
    busy = busy_profiles()
    out = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(ROOT)
        if len(rel.parts) < 2 or any(p.startswith(".") for p in rel.parts):
            continue
        name = path.name
        if name in SKIP_NAMES or name.endswith(SKIP_SUFFIX):
            continue
        st = path.stat()
        if st.st_size == 0 or now - st.st_mtime < MIN_AGE or str(path) in opened:
            continue
        is_live = path.suffix.lower() in VIDEO and "stories" not in rel.parts
        if is_live and ("*" in busy or rel.parts[0] in busy):
            continue
        out.append((path, rel, st.st_size))
    return out


def upload_one(path, rel, size, dry):
    drel = drive_rel(rel)
    dest = f"{REMOTE}/{drel.as_posix()}"
    if dry:
        log(f"DRY would upload {rel} ({size} B) -> {dest}")
        return None
    digest = md5(path)
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
    if path.stat().st_size != size:
        log(f"SKIP delete {rel}: changed during upload")
        return False
    m = ID_RE.search(path.name)
    entry = {"account": rel.parts[0], "item_id": m.group(1) if m else None,
             "kind": drel.parts[1] if len(drel.parts) > 2 else "",
             "path": str(path), "size": size, "md5": digest, "drive_path": dest,
             "uploaded_at": datetime.now(timezone.utc).isoformat()}
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    path.unlink()
    log(f"OK {rel} -> {dest} ({size} B md5 {digest}) local removed")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", help="substring filter on relative path")
    a = ap.parse_args()
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run holds the lock; exit"); return 0
    items = candidates()
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
