#!/usr/bin/env python3
"""Offload finished Douyin captures to Google Drive (rclone remote saridq:).

Only finished files: mtime stable >= MIN_AGE, not open by any process (lsof),
not referenced by an active recorder session.json, size+mtime unchanged across
two scans >= STABLE_SECS apart (observations persist in STABILITY between timer
runs), and no live-video upload for a profile that status.json shows
recording/finalizing. A busy, growing or changed file is a logged skip (exit 0)
retried next tick; only real failures (rclone/auth, verify mismatch on an
unchanged file, journal errors) make the run exit 1.
Each file: rclone copyto -> rclone check + independent md5 -> fsync receipt
-> delete unchanged local file. A crash can leave both copies, never neither.
flock prevents overlapping runs.
"""
import argparse, fcntl, hashlib, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Overrides exist only for dry runs against a scratch tree.
REPO = Path(os.environ.get("DOUYIN_UPLOADER_REPO") or HERE.parent)
STATE_DIR = Path(os.environ.get("DOUYIN_UPLOADER_STATE_DIR") or HERE)
ROOT = REPO / "downloads"
STATUS = REPO / "src" / "status.json"
LOG = STATE_DIR / "uploaded.jsonl"
LOCK = STATE_DIR / "uploader.lock"
STABILITY = STATE_DIR / "upload_stability.json"
REMOTE = os.environ.get("DOUYIN_DRIVE_REMOTE", "saridq:Douyin")
MIN_AGE = 300
STABLE_SECS = 60
ACTIVE_DONE = {"complete", "failed"}
SOURCE_CHANGED_RE = re.compile(r"source file is being updated|source.*(?:size|modtime|mod time).*changed", re.IGNORECASE)
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


def active_session_paths():
    """Exact files an unfinished recorder session still owns (parts, outputs)."""
    owned = set()
    for manifest in ROOT.glob("*/.recording_sessions/*/session.json"):
        try:
            d = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue  # a torn manifest gates nothing here; status.json still does
        # No status = not a live session record (publication plans are gated separately).
        if not isinstance(d, dict) or not d.get("status") or str(d["status"]) in ACTIVE_DONE:
            continue
        values = [d.get("final_output"), *(d.get("final_outputs") or []), *(d.get("parts") or [])]
        owned.update(str(Path(v).resolve()) for v in values if isinstance(v, str) and v)
    return owned


def load_stability():
    try:
        d = json.loads(STABILITY.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stability(state):
    tmp = STABILITY.with_name(STABILITY.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STABILITY)


def observe(state, path, st, now):
    """True once size+mtime (same inode) match an observation >= STABLE_SECS old."""
    key = str(path)
    stamp = [st.st_ino, st.st_size, st.st_mtime_ns]
    seen = state.get(key)
    if isinstance(seen, dict) and seen.get("stamp") == stamp and isinstance(seen.get("at"), (int, float)):
        return now - seen["at"] >= STABLE_SECS
    state[key] = {"stamp": stamp, "at": now}
    return False


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
            and ("*" in busy or rel.parts[0] in busy or publication_pending(path)))


def publication_pending(path):
    """A batch's durable gate keeps its destinations private until commit."""
    # A split member belongs to the gate for its original, unsuffixed stem.
    stems = {path.stem}
    if path.suffix == ".mkv":
        stems.add(re.sub(r"\.part[0-9]{2,}$", "", path.stem))
    gate_names = {f".publication-{hashlib.sha256(stem.encode('utf-8')).hexdigest()}.publication.json"
                  for stem in stems}
    for gate in path.parent.glob(".*.publication.json"):
        try:
            payload = json.loads(gate.read_text(encoding="utf-8"))
            destinations = payload["destinations"]
            if (not isinstance(destinations, list) or not destinations
                    or not all(isinstance(p, str) and p for p in destinations)):
                raise ValueError("Invalid publication destinations")
            if any(Path(destination) == path.resolve() for destination in destinations):
                # A crash after durable commit can leave the gate behind.
                # Only this batch's matching committed plan can release it.
                try:
                    plan = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8-sig"))["publication_plan"]
                    if (plan["state"] == "committed"
                            and [member["destination"] for member in plan["members"]] == destinations):
                        continue
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                return True
        except (OSError, ValueError, KeyError, TypeError):
            # The filename identifies this batch even if its contents are torn.
            if gate.name in gate_names:
                return True
            if re.fullmatch(r"\.publication-[0-9a-f]{64}\.publication\.json", gate.name):
                continue
            # Retain compatibility with the earlier stem-based gate names.
            stem = gate.name[1:-len(".publication.json")]
            if path.name == stem + ".mkv" or path.name.startswith(stem + ".part"):
                return True
    # Compatibility with saved plans from before independent gates existed.
    for manifest in (path.parent / ".recording_sessions").glob("*/session.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
            plan = payload.get("publication_plan")
            if plan and plan.get("state") != "committed":
                if any(Path(member["destination"]) == path.resolve() for member in plan["members"]):
                    return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return False


def candidates():
    now = time.time()
    opened = open_files()
    busy = busy_profiles()
    owned = active_session_paths()
    state, seen = load_stability(), set()
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
            if st.st_size == 0:
                continue
            seen.add(str(path))
            stable = observe(state, path, st, now)
            if str(path) in opened or str(path.resolve()) in owned:
                log(f"INFO SKIP {rel}: still open/owned by the recorder; retry next tick")
                continue
            if now - st.st_mtime < MIN_AGE:
                log(f"INFO WAIT {rel}: modified {int(now - st.st_mtime)}s ago (< {MIN_AGE}s)")
                continue
            if is_busy_live(path, rel, busy):
                log(f"INFO SKIP {rel}: recorder busy (recording/finalizing); retry next tick")
                continue
            if not stable:
                log(f"INFO WAIT {rel}: size/mtime not yet stable across two checks >= {STABLE_SECS}s apart")
                continue
            out.append((path, rel, st.st_size))
        except OSError:
            # Recorder cleanup/rename can race a directory scan.
            continue
    try:
        save_stability({k: v for k, v in state.items() if k in seen})
    except OSError as exc:
        log(f"WARN could not save {STABILITY.name}: {exc}")
    return out


def upload_one(path, rel, size, dry):
    """True uploaded, None skipped (busy/changed: retry next tick), False failed."""
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
            or str(path) in open_files() or is_busy_live(path, rel, busy_profiles())
            or str(path.resolve()) in active_session_paths()):
        log(f"WARN SKIP {rel}: no longer eligible (busy or changed); retry next tick")
        return None
    digest = md5(path)
    if file_stamp(path) != stamp:
        log(f"WARN SKIP {rel}: changed while hashing; retry next tick")
        return None
    # Stall guard: a hung Drive upload must not hold the flock forever.
    # Overall cap: 30 min + 5 min/GiB (expected ~0.5 min/GiB).
    cap = 1800 + int(size / (1 << 30) * 300)
    try:
        r = subprocess.run(["rclone", "copyto", "--retries", "3", "--timeout", "5m",
                            "--contimeout", "60s", "--low-level-retries", "10",
                            str(path), dest], capture_output=True, text=True, timeout=cap)
    except subprocess.TimeoutExpired:
        log(f"FAIL copy {rel}: rclone exceeded {cap}s; will retry next run")
        return False
    if r.returncode:
        if SOURCE_CHANGED_RE.search(r.stderr or "") or not path.is_file() or file_stamp(path) != stamp:
            log(f"WARN SKIP {rel}: source changed during copy; retry next tick: {r.stderr.strip()[-200:]}")
            return None
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
        if not path.is_file() or file_stamp(path) != stamp:
            log(f"WARN SKIP {rel}: local file changed during verify; retry next tick")
            return None
        log(f"FAIL verify {rel}: check rc={c.returncode} remote_md5={remote_md5} local={digest}")
        return False
    if (path.is_symlink() or file_stamp(path) != stamp or str(path) in open_files()
            or is_busy_live(path, rel, busy_profiles()) or str(path.resolve()) in active_session_paths()):
        log(f"WARN SKIP delete {rel}: changed or busy during upload; kept local, retry next tick")
        return None
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
            or is_busy_live(path, rel, busy_profiles()) or str(path.resolve()) in active_session_paths()):
        log(f"WARN SKIP delete {rel}: changed or opened while journaling; kept local, retry next tick")
        return None
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
