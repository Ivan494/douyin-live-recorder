#!/usr/bin/env python3
"""Map the approved 17-profile chart onto a local profiles.json.

The script changes only ``auto_download_videos``, ``auto_download_stories``,
and, on the five active media profiles, a missing ``media_not_before`` cutoff.
It preserves every other key, profile order, and downloaded-state file.
It does not restart a service, probe a room, download media, or upload.

Coordinator sequence, and only while capture and finalizers are idle. If
anything is recording or finalizing, do not run ``--apply`` and do not restart:

1. Copy the reviewed source files into the deployed tree. Do not replace
   ``uploader/drive_uploader.py``, its unit, or its timer.
2. Drift check, which does not write::

       python scripts/apply_chart_media_policy.py --check /path/to/src/profiles.json

   A mismatch of profile ids, ``enabled``, or ``record_live`` stops the
   transaction. Ordinary works/stories flags are the values this command is
   allowed to change, so those differences are reported by ``--print-policy``
   and are not drift.
3. Back up outside the watched downloads directory and apply once. Omit
   ``--activated-at`` so T is the actual UTC instant of this invocation.
   A timestamp more than two minutes from now is refused, which blocks a
   historical backfill::

       python scripts/apply_chart_media_policy.py --apply /path/to/src/profiles.json \\
           --backup-dir /var/backups/douyin-chart-policy

4. Read the effective 17-row policy without downloading::

       python scripts/apply_chart_media_policy.py --print-policy /path/to/src/profiles.json

5. Restart the headless service only after checking again that no capture or
   finalizer is active. This script never restarts it. Re-running ``--apply``
   keeps an existing valid cutoff and does not move T. Disabled profiles do
   not receive a cutoff; enabling one later stays media-gated until that
   profile gets its own new ``media_not_before``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import media_collection_policy as policy


ACTIVATION_SKEW_SECONDS = 120


def _load_profiles(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read profiles JSON: {exc}") from exc
    return payload


def _activation_moment(text):
    if text:
        moment = policy.parse_activated_at(text)
        if moment is None:
            raise SystemExit("activated-at must be a timezone-aware ISO-8601 instant")
    else:
        moment = datetime.now(timezone.utc)
    skew = abs((moment - datetime.now(timezone.utc)).total_seconds())
    if skew > ACTIVATION_SKEW_SECONDS:
        raise SystemExit(
            "Refusing a cutoff that is not the actual activation instant. "
            "Omit --activated-at so T is recorded now; historical backfill is not allowed."
        )
    return moment


def _backup_and_write(path, profiles, backup_dir):
    path = Path(path)
    original = path.read_bytes()
    destination = Path(backup_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = destination / f"profiles.json.{stamp}.bak"
    with backup.open("wb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    text = json.dumps(profiles, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Exit 2 on identity, enabled, or record_live drift. Do not write.")
    mode.add_argument("--print-policy", action="store_true", help="Print the effective 17-row policy. Do not write or download.")
    mode.add_argument("--apply", action="store_true", help="Back up and update chart flags when the drift check passes.")
    parser.add_argument("profiles", help="Path to profiles.json")
    parser.add_argument("--backup-dir", help="Directory outside the downloads tree. Required with --apply.")
    parser.add_argument("--activated-at", help="Timezone-aware activation instant. Omit to use now. Past dates are refused.")
    args = parser.parse_args(argv)
    profiles = _load_profiles(args.profiles)
    if args.print_policy:
        print(json.dumps(policy.media_policy_document(profiles), ensure_ascii=False, indent=2))
        return 0
    if args.check:
        problems = policy.identity_drift(profiles)
        if problems:
            print(json.dumps({"ok": False, "drift": problems}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps({"ok": True, "drift": []}, ensure_ascii=False, indent=2))
        return 0
    if not args.backup_dir:
        raise SystemExit("--apply requires --backup-dir outside the watched downloads directory")
    moment = _activation_moment(args.activated_at)
    try:
        updated, report = policy.apply_chart_policy(profiles, moment)
    except policy.ChartDrift as exc:
        print(json.dumps({"ok": False, "drift": exc.problems}, ensure_ascii=False, indent=2))
        return 2
    backup = _backup_and_write(args.profiles, updated, args.backup_dir)
    print(json.dumps({"ok": True, "backup": str(backup), **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
