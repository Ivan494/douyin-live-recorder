"""Per-profile works/stories gates and the forward-only activation cutoff.

Headless collection reads ``auto_download_videos`` and ``auto_download_stories``
independently. ``enabled`` false closes media and livestreams. Turning both
media flags off does not change ``record_live``. A profile that wants media
can transfer a new work or story only when that item's authoritative
``create_time`` is at or after the profile's own ``media_not_before`` instant.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path


CHART_FIXTURE = Path(__file__).with_name("chart_17_profiles.json")
MEDIA_NOT_BEFORE = "media_not_before"
_FLAG_KEYS = ("enabled", "auto_download_videos", "auto_download_stories", "record_live")


class ChartDrift(ValueError):
    """The profiles file is not the approved 17-row identity baseline."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def load_chart_rows(path=CHART_FIXTURE):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 17:
        raise ValueError("chart fixture must contain 17 profiles")
    seen = set()
    normalized = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or row.get("row") != index or not row.get("id"):
            raise ValueError("chart fixture rows must be contiguous and keyed by id")
        if row["id"] in seen:
            raise ValueError("chart fixture profile ids must be unique")
        seen.add(row["id"])
        item = {"row": index, "id": row["id"], "name": row.get("name", "")}
        for key in _FLAG_KEYS:
            if not isinstance(row.get(key), bool):
                raise ValueError(f"chart fixture {row['id']} {key} must be boolean")
            item[key] = row[key]
        normalized.append(item)
    return tuple(normalized)


CHART_ROWS = load_chart_rows()
CHART_BY_ID = {row["id"]: row for row in CHART_ROWS}
ACTIVE_MEDIA_IDS = frozenset(
    row["id"]
    for row in CHART_ROWS
    if row["enabled"] and row["auto_download_videos"] and row["auto_download_stories"]
)


def parse_activated_at(value):
    """Return an aware datetime, or None when the value is not an unambiguous instant."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return None
    return moment


def parse_media_not_before(value):
    """Unix seconds for a timezone-aware cutoff, else None."""
    moment = parse_activated_at(value)
    if moment is None:
        return None
    return moment.timestamp()


def format_activated_at(moment):
    if parse_activated_at(moment) is None:
        raise ValueError("activated_at must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def authoritative_create_time(aweme):
    """Unix seconds from the item's own create_time. Missing or unusable values are None.

    Only ``create_time`` is authoritative. Update times, client clocks, and a
    missing field are not substituted, because that would widen the window.
    """
    if not isinstance(aweme, dict) or "create_time" not in aweme:
        return None
    value = aweme.get("create_time")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def media_cutoff_enforced(profile):
    """True when this profile carries a cutoff field and item filtering must run."""
    return isinstance(profile, dict) and MEDIA_NOT_BEFORE in profile


def item_cutoff_skip_reason(profile, aweme):
    """None when the item may be transferred. Otherwise a policy-skip reason.

    Skipped items are not successes, failures, or completion records.
    """
    cutoff = parse_media_not_before(profile.get(MEDIA_NOT_BEFORE) if isinstance(profile, dict) else None)
    if cutoff is None:
        return "invalid_cutoff"
    created = authoritative_create_time(aweme)
    if created is None:
        if not isinstance(aweme, dict) or "create_time" not in aweme or aweme.get("create_time") in (None, ""):
            return "missing_create_time"
        return "invalid_create_time"
    if created < cutoff:
        return "before_cutoff"
    return None


def _stored_bool(profile, key, default):
    if key not in profile:
        return default, None
    value = profile.get(key)
    if isinstance(value, bool):
        return value, None
    return None, f"{profile.get('id')}: {key} is not a boolean"


def effective_row(profile):
    """Scheduling result for one stored profile. Livestreams ignore the media cutoff."""
    profile = profile if isinstance(profile, dict) else {}
    enabled, enabled_problem = _stored_bool(profile, "enabled", True)
    videos, videos_problem = _stored_bool(profile, "auto_download_videos", False)
    stories, stories_problem = _stored_bool(profile, "auto_download_stories", False)
    record_live, live_problem = _stored_bool(profile, "record_live", True)
    enabled = bool(enabled)
    videos = bool(videos)
    stories = bool(stories)
    record_live = bool(record_live)
    cutoff = parse_media_not_before(profile.get(MEDIA_NOT_BEFORE)) if MEDIA_NOT_BEFORE in profile else None
    cutoff_open = cutoff is not None
    wants_media = videos or stories
    if not enabled:
        gate = "disabled"
    elif not wants_media:
        gate = "auto_off"
    elif cutoff_open:
        gate = "open"
    else:
        gate = "closed_missing_cutoff"
    return {
        "enabled": enabled,
        "auto_download_videos": videos,
        "auto_download_stories": stories,
        "record_live": record_live,
        "effective_works": bool(enabled and videos and cutoff_open),
        "effective_stories": bool(enabled and stories and cutoff_open),
        "effective_live": bool(enabled and record_live),
        "media_not_before": profile.get(MEDIA_NOT_BEFORE) if cutoff_open else None,
        "media_gate": gate,
        "problems": [item for item in (enabled_problem, videos_problem, stories_problem, live_problem) if item],
    }


def media_policy_document(profiles):
    """Machine-readable 17-row view. Does not contact the network or read media files."""
    rows = []
    drift = []
    present = {}
    if not isinstance(profiles, list):
        drift.append("profiles must be a list")
        profiles = []
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("id"):
            drift.append("profile missing id")
            continue
        profile_id = str(profile["id"])
        if profile_id in present:
            drift.append(f"duplicate id {profile_id}")
            continue
        present[profile_id] = profile
    for spec in CHART_ROWS:
        profile = present.get(spec["id"])
        if profile is None:
            drift.append(f"missing {spec['id']}")
            continue
        row = effective_row(profile)
        row.update({"row": spec["row"], "id": spec["id"], "name": profile.get("name", "")})
        row["chart_mismatch"] = [key for key in _FLAG_KEYS if row[key] != spec[key]]
        rows.append(row)
        drift.extend(row["problems"])
    extra_ids = sorted(set(present) - set(CHART_BY_ID))
    for profile_id in extra_ids:
        drift.append(f"unexpected profile {profile_id}")
    counts = {
        "profiles": len(rows),
        "enabled": sum(row["enabled"] for row in rows),
        "stored_videos_on": sum(row["auto_download_videos"] for row in rows),
        "stored_stories_on": sum(row["auto_download_stories"] for row in rows),
        "effective_works": sum(row["effective_works"] for row in rows),
        "effective_stories": sum(row["effective_stories"] for row in rows),
        "effective_live": sum(row["effective_live"] for row in rows),
        "media_gate_open": sum(row["media_gate"] == "open" for row in rows),
    }
    return {"rows": rows, "counts": counts, "drift": drift, "extra_ids": extra_ids}


def identity_drift(profiles):
    """Differences that must stop a chart write. Works/stories flag edits are not drift."""
    return chart_drift_problems(profiles)


def chart_drift_problems(profiles):
    problems = []
    if not isinstance(profiles, list):
        return ["profiles must be a list"]
    seen = set()
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("id"):
            problems.append("profile missing id")
            continue
        if profile["id"] in seen:
            problems.append(f"duplicate id {profile['id']}")
        seen.add(profile["id"])
    for profile_id in sorted(set(CHART_BY_ID) - seen):
        problems.append(f"missing {profile_id}")
    for profile_id in sorted(seen - set(CHART_BY_ID)):
        problems.append(f"unexpected profile {profile_id}")
    by_id = {}
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("id") and profile["id"] not in by_id:
            by_id[profile["id"]] = profile
    for spec in CHART_ROWS:
        profile = by_id.get(spec["id"])
        if profile is None:
            continue
        for key in ("enabled", "record_live"):
            if key in profile and not isinstance(profile.get(key), bool):
                problems.append(f"{spec['id']}: {key} is not a boolean")
                continue
            actual = profile.get(key, True)
            if actual != spec[key]:
                problems.append(f"{spec['id']}: {key} is {actual} and the chart keeps {spec[key]}")
        if spec["id"] in ACTIVE_MEDIA_IDS and MEDIA_NOT_BEFORE in profile:
            if parse_media_not_before(profile.get(MEDIA_NOT_BEFORE)) is None:
                problems.append(f"{spec['id']}: media_not_before is present but not a timezone-aware instant")
    return problems


def apply_chart_policy(profiles, activated_at):
    """Return a new profile list with only chart flags and required cutoffs changed.

    ``activated_at`` is stored only on the five active media profiles that do
    not already have a valid cutoff. A second call does not move that instant.
    Disabled and auto-off profiles do not keep a cutoff. Raises ChartDrift
    without producing a deployable list when identity, enabled, record_live,
    or an existing active cutoff is unsafe.
    """
    moment = parse_activated_at(activated_at)
    if moment is None:
        raise ValueError("activated_at must be a timezone-aware datetime or ISO-8601 instant")
    problems = chart_drift_problems(profiles)
    if problems:
        raise ChartDrift(problems)
    stamp = format_activated_at(moment)
    updated = []
    report = {"cutoffs_set": [], "cutoffs_kept": [], "cutoffs_removed": [], "flag_updates": []}
    for profile in profiles:
        item = copy.deepcopy(profile)
        spec = CHART_BY_ID[item["id"]]
        flag_changed = False
        for key in ("auto_download_videos", "auto_download_stories"):
            if item.get(key) != spec[key]:
                flag_changed = True
            item[key] = spec[key]
        if flag_changed:
            report["flag_updates"].append(spec["id"])
        if spec["id"] in ACTIVE_MEDIA_IDS:
            if MEDIA_NOT_BEFORE in item and parse_media_not_before(item.get(MEDIA_NOT_BEFORE)) is not None:
                report["cutoffs_kept"].append(spec["id"])
            else:
                item[MEDIA_NOT_BEFORE] = stamp
                report["cutoffs_set"].append(spec["id"])
        elif MEDIA_NOT_BEFORE in item:
            item.pop(MEDIA_NOT_BEFORE, None)
            report["cutoffs_removed"].append(spec["id"])
        updated.append(item)
    report["activated_at"] = stamp if report["cutoffs_set"] else None
    return updated, report
