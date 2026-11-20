"""Chart matrix, forward-only cutoff, and headless scheduling. No live media I/O."""

import hashlib
import importlib.util
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import douyin_recorder_app as app
import douyin_headless as headless
import douyin_media_downloader as media
import media_collection_policy as policy

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: "")
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "")
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
T0_UNIX = int(T0.timestamp())
VIDEO_BODY = b"\x00\x00\x00\x18ftypisom" + b"v" * 4096
ACTIVE_IDS = (
    "zhuni-zhea",
    "e55ff90f-80ff-4cd9-90cc-8bb4c28f239f",
    "e2219a44-798e-4dd1-baef-58482e885307",
    "59280f26-4f18-4628-865d-d2a652e46f7e",
    "6a3124fe-4427-4f5c-8eb5-f718e8a819ac",
)
DORMANT_IDS = (
    "338c550f-9fb1-428e-adc2-70ef6535e4a9",
    "a61a2e7e-f184-49ef-8ad1-f3cd18259534",
)
PRESERVED_HASHES = {
    "uploader/drive_uploader.py": "60a19c656a48396b8953ade6d9e80323b802590cbe6d0fca1671019d5b510d56",
    "uploader/douyin-drive-upload.service": "d28a433242606af9aaa55757dc715b4a44c504ce243ceff07769b5cbf495411c",
    "uploader/douyin-drive-upload.timer": "40404f28125a5ddd7ed11772fab03213caf3fab138fe5f20cb594e7f09b98ce3",
    "src/recording_segments.py": "592128c6ad8c237ca58acb8882fedba0971aacf74628c118372c3438ac4dbbb8",
    "src/douyin_live_watcher.py": "415409e36a97bf0ac8258a8d0123c2770500e7cfd167dde95198216d90113a3e",
    "tests/test_capture_finalizer.py": "8f1cb64626718a1d7e624c60a9ce8c84116ff525b1e69f0d7617f4228b16e177",
    "tests/test_finalize_retry.py": "99cc12518c665402ec76decb1172c976eb75d1d7bb9cc5215b1312733328cf73",
    "tests/test_drive_uploader.py": "dc51d07343aeb3555c1734862555a2275ecffb8b7ac40fa3b73cdad7b28d5926",
}


def script_module():
    path = REPO / "scripts" / "apply_chart_media_policy.py"
    spec = importlib.util.spec_from_file_location("apply_chart_media_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployed_current_profiles():
    """Sanitized stand-in for the pre-chart host: every video flag off, every story flag on."""
    profiles = []
    for spec in policy.CHART_ROWS:
        profiles.append({
            "id": spec["id"],
            "name": spec["name"],
            "enabled": spec["enabled"],
            "record_live": spec["record_live"],
            "auto_download_videos": False,
            "auto_download_stories": True,
            "priority": spec["row"] in {1, 2, 3, 16, 17},
            "url": f"https://live.douyin.com/{700000 + spec['row']}",
            "original_profile_url": f"https://www.douyin.com/user/USER_ID_ROW_{spec['row']}",
            "output_dir": f"/data/downloads/{spec['id']}",
            "quality": "OD",
            "stream_orientation": 1,
            "custom_note": {"kept": spec["row"]},
        })
    return profiles


def write_profiles(path, profiles):
    path.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def drive_one_cycle(engine):
    def wait(timeout=None):
        if timeout == 1:
            engine.stop_event.set()
        return False

    engine.wake_event.wait = wait
    engine._run()


def media_engine(profiles):
    store = SimpleNamespace(
        profiles=profiles,
        settings={"media_poll_interval_seconds": 60, "poll_jitter_seconds": 0, "priority_poll_jitter_seconds": 0},
        get_profile=lambda profile_id: next((item for item in profiles if item["id"] == profile_id), None),
    )
    disk = SimpleNamespace(can_start=lambda _profile: True)
    engine = headless.HeadlessStoryEngine(store, app.RecorderEventQueue(), disk)
    engine._save_circuit_breaker_state = lambda: None
    return engine


def aweme(aweme_id, create_time="missing", **extra):
    item = {
        "aweme_id": aweme_id,
        "desc": aweme_id,
        "video": {"play_addr": {"url_list": [f"https://video-web-cn.douyin.com/{aweme_id}.mp4"]}},
    }
    if create_time != "missing":
        item["create_time"] = create_time
    item.update(extra)
    return item


def transfer_items(tmp_path, profile, items, state, kind):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, profile, items, tmp_path, state, kind)
    return result, calls


def test_fixture_matches_approved_chart_file():
    fixture = json.loads((REPO / "src" / "chart_17_profiles.json").read_text(encoding="utf-8"))
    assert hashlib.sha256((REPO / "src" / "chart_17_profiles.json").read_bytes()).hexdigest() == (
        "5e259ef26d0653b67a992994edcedebe345d74a200176354e77c3165c3b3ce57"
    )
    assert [dict(row) for row in policy.CHART_ROWS] == [
        {key: item[key] for key in ("row", "id", "name", "enabled", "auto_download_videos", "auto_download_stories", "record_live")}
        for item in fixture["profiles"]
    ]
    assert policy.ACTIVE_MEDIA_IDS == set(ACTIVE_IDS)
    names = {row["id"]: row["name"] for row in policy.CHART_ROWS}
    assert names["zhuni-zhea"] == "猪呢.《浙A》"
    assert names["e55ff90f-80ff-4cd9-90cc-8bb4c28f239f"] == "缓（）"
    assert names["59280f26-4f18-4628-865d-d2a652e46f7e"] == "陈皮兔o(一ω一)o"
    assert names["6a3124fe-4427-4f5c-8eb5-f718e8a819ac"] == "@虎牙钉"


def test_migration_is_idempotent_and_preserves_unrelated_state(tmp_path):
    profiles = deployed_current_profiles()
    state_path = tmp_path / "douyin_media_state.json"
    state_path.write_text(json.dumps({"downloaded_video_ids": ["prior-video"], "downloaded_story_ids": ["prior-story"]}), encoding="utf-8")
    original_state = state_path.read_bytes()
    later = T0 + timedelta(days=3)
    updated, report = policy.apply_chart_policy(profiles, T0)
    again, second = policy.apply_chart_policy(updated, later)
    assert state_path.read_bytes() == original_state
    assert profiles[0]["auto_download_videos"] is False  # input is not mutated
    assert report["cutoffs_set"] == list(ACTIVE_IDS)
    assert second["cutoffs_set"] == [] and second["cutoffs_kept"] == list(ACTIVE_IDS)
    stamps = []
    for before, after in zip(profiles, again):
        spec = policy.CHART_BY_ID[before["id"]]
        assert after["enabled"] is before["enabled"] is spec["enabled"]
        assert after["record_live"] is before["record_live"] is spec["record_live"]
        assert after["auto_download_videos"] is spec["auto_download_videos"]
        assert after["auto_download_stories"] is spec["auto_download_stories"]
        for key in ("name", "priority", "url", "original_profile_url", "output_dir", "quality", "stream_orientation", "custom_note"):
            assert after[key] == before[key]
        if spec["id"] in ACTIVE_IDS:
            stamps.append(after["media_not_before"])
            assert again[profiles.index(before)]["media_not_before"] == after["media_not_before"]
        else:
            assert "media_not_before" not in after
    assert stamps == [policy.format_activated_at(T0)] * 5
    document = policy.media_policy_document(json.loads(json.dumps(again)))
    assert document["drift"] == []
    assert document["counts"] == {
        "profiles": 17,
        "enabled": 14,
        "stored_videos_on": 7,
        "stored_stories_on": 7,
        "effective_works": 5,
        "effective_stories": 5,
        "effective_live": 14,
        "media_gate_open": 5,
    }
    by_id = {row["id"]: row for row in document["rows"]}
    assert [row["row"] for row in document["rows"]] == list(range(1, 18))
    for profile_id in ACTIVE_IDS:
        assert by_id[profile_id]["media_gate"] == "open"
        assert by_id[profile_id]["effective_works"] and by_id[profile_id]["effective_stories"] and by_id[profile_id]["effective_live"]
    for profile_id in DORMANT_IDS:
        row = by_id[profile_id]
        assert row["enabled"] is False and row["auto_download_videos"] and row["auto_download_stories"]
        assert row["media_gate"] == "disabled"
        assert not row["effective_works"] and not row["effective_stories"] and not row["effective_live"]
    auto_off = [row for row in document["rows"] if not row["auto_download_videos"] and not row["auto_download_stories"]]
    assert len(auto_off) == 10
    assert sum(row["media_gate"] == "auto_off" and row["effective_live"] for row in auto_off) == 9
    disabled_off = next(row for row in auto_off if row["id"] == "f8108c74-ea7e-4e40-a3a4-e515326e1b01")
    assert disabled_off["media_gate"] == "disabled" and disabled_off["effective_live"] is False


def test_drift_and_historical_cutoff_do_not_write(tmp_path):
    tool = script_module()
    path = tmp_path / "profiles.json"
    profiles = deployed_current_profiles()
    write_profiles(path, profiles)
    original = path.read_bytes()
    drifted = json.loads(original)
    drifted[4]["enabled"] = True
    drifted_path = tmp_path / "drift.json"
    write_profiles(drifted_path, drifted)
    assert tool.main(["--check", str(drifted_path)]) == 2
    assert drifted_path.read_bytes() == json.dumps(drifted, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    with pytest.raises(SystemExit):
        tool.main(["--apply", str(path), "--backup-dir", str(tmp_path / "backups"), "--activated-at", "2020-01-01T00:00:00+00:00"])
    assert path.read_bytes() == original
    profiles[0]["media_not_before"] = "not-a-timestamp"
    with pytest.raises(policy.ChartDrift):
        policy.apply_chart_policy(profiles, T0)
    dormant = deployed_current_profiles()
    dormant[4]["media_not_before"] = policy.format_activated_at(T0 - timedelta(days=30))
    cleaned, report = policy.apply_chart_policy(dormant, T0)
    assert report["cutoffs_removed"] == [DORMANT_IDS[0]]
    assert "media_not_before" not in cleaned[4]


def test_apply_command_backs_up_and_prints_policy(tmp_path):
    tool = script_module()
    path = tmp_path / "profiles.json"
    write_profiles(path, deployed_current_profiles())
    backup_dir = tmp_path / "outside-downloads"
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    assert tool.main([
        "--apply", str(path), "--backup-dir", str(backup_dir), "--activated-at", moment.isoformat(),
    ]) == 0
    backups = list(backup_dir.glob("profiles.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))[0]["auto_download_videos"] is False
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert tool.main(["--print-policy", str(path)]) == 0
    assert tool.main(["--check", str(path)]) == 0
    document = policy.media_policy_document(loaded)
    assert document["counts"]["effective_works"] == 5
    assert document["counts"]["effective_live"] == 14
    assert {row["media_not_before"] for row in document["rows"] if row["id"] in ACTIVE_IDS} == {policy.format_activated_at(moment)}
    # A restart sees the same persisted instant and does not advance it.
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    _again, report = policy.apply_chart_policy(reloaded, moment + timedelta(hours=5))
    assert report["cutoffs_kept"] == list(ACTIVE_IDS)
    assert [item.get("media_not_before") for item in _again if item["id"] in ACTIVE_IDS] == [policy.format_activated_at(moment)] * 5


def test_scheduler_honors_disabled_auto_off_and_independent_kinds(monkeypatch):
    monkeypatch.setattr(app, "HEADLESS", True)
    profiles, _report = policy.apply_chart_policy(deployed_current_profiles(), T0)
    download = Mock(return_value={"videos": {"status": "ok", "downloaded": 0}, "stories": {"status": "ok", "downloaded": 0}})
    monkeypatch.setattr(app, "download_profile", download)
    engine = media_engine(profiles)
    drive_one_cycle(engine)
    assert sorted(call.args[0]["id"] for call in download.call_args_list) == sorted(ACTIVE_IDS)
    for call in download.call_args_list:
        assert call.kwargs["videos"] is True and call.kwargs["stories"] is True

    download.reset_mock()
    for profile in profiles:
        if profile["id"] in DORMANT_IDS or not profile["auto_download_videos"]:
            engine._check_profile(profile)
    download.assert_not_called()

    works = {
        "id": "works-only", "name": "works-only", "enabled": True, "record_live": True,
        "auto_download_videos": True, "auto_download_stories": False,
        "media_not_before": policy.format_activated_at(T0), "url": "https://live.douyin.com/1",
    }
    stories = dict(works, id="stories-only", name="stories-only", auto_download_videos=False, auto_download_stories=True)
    both_off = dict(works, id="auto-off", name="auto-off", auto_download_videos=False, auto_download_stories=False)
    missing = dict(works, id="missing-cutoff")
    missing.pop("media_not_before")
    kind_engine = media_engine([works, stories, both_off, missing])
    drive_one_cycle(kind_engine)
    kinds = {call.args[0]["id"]: call.kwargs for call in download.call_args_list}
    assert set(kinds) == {"works-only", "stories-only"}
    assert kinds["works-only"]["videos"] is True and kinds["works-only"]["stories"] is False
    assert kinds["stories-only"]["videos"] is False and kinds["stories-only"]["stories"] is True


def test_download_profile_does_not_cross_fetch_kinds(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "_mobile_cookie_header", lambda: "")
    seen = []

    def fake_download(*_args, **kwargs):
        seen.append(kwargs.get("media_kind") or _args[5])
        return media.MediaResult()

    posts = Mock(return_value=[{"aweme_id": "work-1", "create_time": T0_UNIX + 1}])
    stories = Mock(return_value=([{"aweme_id": "story-1", "create_time": T0_UNIX + 1}], "fixture", True))
    monkeypatch.setattr(media, "download_aweme_items", fake_download)
    monkeypatch.setattr(media, "fetch_posts", posts)
    monkeypatch.setattr(media, "fetch_stories", stories)
    monkeypatch.setattr(media, "fetch_posts_via_browser", Mock(side_effect=AssertionError("browser fallback")))
    monkeypatch.setattr(media, "fetch_posts_via_mobile_api", Mock(side_effect=AssertionError("mobile fallback")))
    profile = {
        "id": "kind",
        "name": "kind",
        "enabled": True,
        "output_dir": str(tmp_path),
        "original_profile_url": "https://www.douyin.com/user/MS4wLjABAAAA_TEST_ONLY",
        "media_not_before": policy.format_activated_at(T0),
    }
    media.download_profile(profile, {}, videos=True, stories=False)
    assert posts.called and not stories.called
    assert seen == ["video"]
    posts.reset_mock()
    stories.reset_mock()
    seen.clear()
    media.download_profile(profile, {}, videos=False, stories=True)
    assert stories.called and not posts.called
    assert seen == ["story"]


def test_live_scheduler_keeps_auto_off_and_skips_disabled():
    profiles, _report = policy.apply_chart_policy(deployed_current_profiles(), T0)
    store = SimpleNamespace(
        profiles=profiles,
        settings={"poll_interval_seconds": 30, "poll_jitter_seconds": 0, "priority_poll_jitter_seconds": 0},
        get_profile=lambda profile_id: next((item for item in profiles if item["id"] == profile_id), None),
    )
    engine = app.MonitorEngine(store, app.RecorderEventQueue())
    checked = []
    engine._poll_processes = lambda: None
    engine._check_profile = lambda profile: checked.append(profile["id"])
    drive_one_cycle(engine)
    assert sorted(checked) == sorted(profile["id"] for profile in profiles if profile["enabled"])
    assert set(DORMANT_IDS).isdisjoint(checked)
    assert "f8108c74-ea7e-4e40-a3a4-e515326e1b01" not in checked
    assert "douyin-922949271615" in checked


def test_forward_only_cutoff_skips_old_and_undated_without_completing(tmp_path, monkeypatch, caplog):
    ledger = tmp_path / "uploaded.jsonl"
    ledger.write_text(json.dumps({"item_id": "uploaded-work", "path": "/archive/uploaded-work.mp4"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("DOUYIN_UPLOADED_LOG", str(ledger))
    profile = {"id": "zhuni-zhea", "name": "猪呢.《浙A》", "media_not_before": policy.format_activated_at(T0)}
    items = [
        aweme("too-old", T0_UNIX - 1),
        aweme("at-cutoff", T0_UNIX),
        aweme("pinned-old", T0_UNIX - 5, is_top=1),
        aweme("missing-time"),
        aweme("zero-time", 0),
        aweme("bad-time", "yesterday"),
        aweme("false-time", False),
        aweme("after-cutoff", T0_UNIX + 1),
        aweme("keep-video", T0_UNIX + 5),
        aweme("keep-story", T0_UNIX + 5),
        aweme("uploaded-work", T0_UNIX + 5),
        aweme("also-new", T0_UNIX + 2),
    ]
    state = {
        "downloaded_video_ids": ["keep-video", "prior-unseen", "old-complete"],
        "downloaded_story_ids": ["keep-story", "prior-story"],
        "anchor": "keep-me",
    }
    caplog.set_level(logging.INFO)
    _result, calls = transfer_items(tmp_path, profile, items, state, "video")
    assert sorted(path.rsplit("/", 1)[-1] for path in calls) == ["after-cutoff.mp4", "also-new.mp4", "at-cutoff.mp4"]
    assert state["downloaded_story_ids"] == ["keep-story", "prior-story"]
    assert state["anchor"] == "keep-me"
    assert set(state["downloaded_video_ids"]) == {
        "keep-video", "prior-unseen", "old-complete", "at-cutoff", "after-cutoff", "also-new",
    }
    for skipped in ("too-old", "pinned-old", "missing-time", "zero-time", "bad-time", "false-time", "uploaded-work", "keep-story"):
        assert skipped not in state["downloaded_video_ids"]
    assert "before_cutoff" in caplog.text
    assert "missing_create_time" in caplog.text
    assert "invalid_create_time" in caplog.text
    state_path = tmp_path / "douyin_media_state.json"
    media.save_state(state_path, state)
    _path, reloaded = media.load_state(tmp_path)
    assert "prior-unseen" in reloaded["downloaded_video_ids"]
    assert "too-old" not in reloaded["downloaded_video_ids"]
    assert "prior-story" in reloaded["downloaded_story_ids"]

    story_state = {"downloaded_video_ids": ["keep-video"], "downloaded_story_ids": ["prior-story"]}
    story_items = [aweme("story-old", T0_UNIX - 1), aweme("story-new", T0_UNIX), aweme("keep-video", T0_UNIX + 1)]
    _story_result, story_calls = transfer_items(tmp_path / "stories-run", profile, story_items, story_state, "story")
    assert [path.rsplit("/", 1)[-1] for path in story_calls] == ["story-new.mp4"]
    assert story_state["downloaded_video_ids"] == ["keep-video"]
    assert set(story_state["downloaded_story_ids"]) == {"prior-story", "story-new"}
    assert "story-old" not in story_state["downloaded_story_ids"]


def test_missing_cutoff_closes_media_without_marking_items(tmp_path):
    profile = {"id": "later-enable", "name": "ciu7", "enabled": True, "auto_download_videos": True, "auto_download_stories": True}
    row = policy.effective_row(profile)
    assert row["media_gate"] == "closed_missing_cutoff"
    assert row["effective_works"] is False and row["effective_live"] is True
    state = {"downloaded_video_ids": ["prior-unseen"]}
    _result, calls = transfer_items(
        tmp_path,
        {"id": "later-enable", "media_not_before": "yesterday"},
        [aweme("brand-new", T0_UNIX + 10)],
        state,
        "video",
    )
    assert calls == []
    assert state["downloaded_video_ids"] == ["prior-unseen"]


def test_legacy_items_without_cutoff_field_still_download(tmp_path):
    state = {"downloaded_video_ids": []}
    _result, calls = transfer_items(tmp_path, {"id": "gui"}, [aweme("legacy", T0_UNIX - 100)], state, "video")
    assert calls and state["downloaded_video_ids"] == ["legacy"]


def test_policy_command_and_validation_do_not_probe(monkeypatch, tmp_path, capsys):
    profiles, _report = policy.apply_chart_policy(deployed_current_profiles(), T0)
    store = SimpleNamespace(
        profiles=profiles,
        settings={"headless_output_root": str(tmp_path), "ffmpeg_path": "ffmpeg", "headless_min_free_gb": 5, "headless_critical_free_gb": 1},
    )
    monkeypatch.setattr(headless.app, "RecorderStore", lambda read_only=True: store)
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", Mock(side_effect=AssertionError("ffmpeg probe")))
    assert headless.main(["--policy"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["counts"]["profiles"] == 17
    assert document["counts"]["effective_works"] == 5
    assert document["counts"]["effective_live"] == 14
    prepared = headless.prepare_store(store)
    assert prepared.profiles[0]["auto_download_videos"] is True
    monkeypatch.setattr(app, "resolve_ffmpeg_executable", lambda _path: "ffmpeg")
    monkeypatch.setattr(app, "resolve_trusted_executable", lambda *_args, **_kwargs: "ffprobe")
    summary = headless.validate(prepared)
    assert summary["post_downloads"] is True
    assert summary["effective_works"] == 5
    assert summary["effective_live"] == 14
    assert summary["live_profiles"] == 14


def test_uploader_and_capture_files_stay_byte_identical():
    for relative, digest in PRESERVED_HASHES.items():
        assert hashlib.sha256((REPO / relative).read_bytes()).hexdigest() == digest
