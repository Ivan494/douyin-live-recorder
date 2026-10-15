# Drive uploader and recorder review

Reviewed `d01409c`, `e92ca48`, and `28fe3a1` on branch `codex/douyin-edge`.

## Findings and fixes

- The uploader could delete a same-size rewrite or replacement after verifying
  an older copy. It now checks inode, size, mtime and ctime around hashing,
  verification and journaling, and rechecks open files and recorder activity.
- Failed/incomplete `lsof` scans were treated as safe. They now stop uploading;
  the normal no-matches result remains supported. Malformed status data blocks
  live uploads, and global active/deferred finalizers block them even when a
  different profile is recording.
- Downloader staging names such as `.mp4.part.<pid>.<suffix>` were eligible for
  offload. Staging files and internal session files are excluded.
- An interrupted JSONL append could swallow the next receipt. Appends now
  separate an unterminated tail and fsync the verified receipt before deletion.
  Command errors, timeouts and journal failures preserve the local copy and
  report failure without stopping later files in the batch.
- Corrupt JSONL types and invalid UTF-8 could abort dedupe. Each line is now
  decoded independently, invalid records/IDs are ignored, and later valid
  receipts remain usable.
- A single uploaded cover, motion clip or metadata sidecar could suppress an
  entire album. Dedupe now accounts for every expected still/motion asset across
  local files and uploaded paths, including edited captions and grouped item
  directories. Valid-looking staging files cannot prove an item is complete.
- Grouped uploads now record the item ID from its directory when filenames omit
  it. Uploader defaults resolve beside the running script instead of pointing
  every worktree at the main checkout.
- The finalizer silently omitted missing parts and could delete raw parts
  without an available FFprobe. Missing/changed parts or unavailable/failed
  validation now preserve raw material. Zero-byte failed-connection parts may
  be omitted only while they remain unchanged; all-empty sessions fail safely.
- Both recorder and finalizer commands select the first video/audio streams.
  The real 2-video/2-audio regression now invokes the production finalizer.

A crash after receipt fsync but before unlink can leave both copies. Retrying
re-verifies the upload; duplicate receipts are harmless to dedupe.

## Files

- `uploader/drive_uploader.py`
- `src/douyin_recorder_app.py`
- `src/douyin_media_downloader.py`
- `tests/test_drive_uploader.py` (new)
- `tests/test_capture_finalizer.py` (new)
- `tests/test_uploaded_dedupe.py` (new)
- `tests/test_single_track_maps.py`
- `src/tests/test_media_downloader.py` (mock the existing CDP/port network probe)
- `DONE.md`

## Validation

- **PASS: 229 tests, 0 failures**, with a socket/DNS guard: **0 network attempts**.
  This includes crash/retry injection, partial copies, rclone errors/timeouts,
  MD5 mismatch, growing/replaced files, real `lsof` checks (including Unicode
  names), locking, corrupt JSONL, dedupe, publication/cleanup failures, headless
  disk guards, and local FFmpeg/FFprobe integration.
- **PASS:** `git diff --check` and Python compilation of all modified Python files.
- The first regression run against the original code had 37 failures and 52
  passes; the exposed defects above were fixed.
- Full GUI collection is unavailable here because `tkinter` is absent. Existing
  HTTP-server recording integration was excluded to obey the no-network rule.
  No live Drive/Douyin endpoint was used.

Reproduce the selected suite with pytest installed in a local environment:

```bash
DOUYIN_HEADLESS=1 PYTHONPATH=src python3 -m pytest -q \
  tests/test_drive_uploader.py tests/test_capture_finalizer.py \
  tests/test_uploaded_dedupe.py tests/test_single_track_maps.py \
  src/tests/test_media_downloader.py src/tests/test_media_asset_repair.py \
  src/tests/test_live_photo_downloads.py src/tests/test_headless_linux.py \
  src/tests/test_reliability.py::ReliabilityTest::test_real_ffmpeg_finalizer_stitches_and_validates_mkv_parts
```

All source changes are confined to this worktree. Services/timers and main
checkout working files were untouched. Configuration, probes and secrets were
excluded from the commit. No push was performed.
