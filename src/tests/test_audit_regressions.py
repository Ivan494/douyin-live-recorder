"""Expected behavior for the September 2026 audit findings."""
import asyncio
import json
from pathlib import Path
import queue
import socket
import sys
import time
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

import douyin_recorder_app as app
import douyin_media_downloader as media


def engine_for(profile):
    store = SimpleNamespace(profiles=[profile], settings=app.default_settings())
    return app.MonitorEngine(store, queue.Queue())


def test_edit_discards_obsolete_room_result():
    profile = {"id": "p", "name": "p", "url": "https://www.douyin.com/user/old"}
    engine = engine_for(profile)

    async def resolving(shared_profile):
        # A GUI save mutates this same dict while the worker awaits HTTP.
        profile["url"] = "https://www.douyin.com/user/new"
        return {"live_url": "https://live.douyin.com/old"}, SimpleNamespace(is_live=False)

    with patch.object(engine, "_resolve", side_effect=resolving), patch.object(app, "save_json"):
        engine._check_profile(profile)
    assert not profile.get("fallback_live_url")


def test_deleted_profile_does_not_start_recording():
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/old"}
    engine = engine_for(profile)

    async def resolving(_profile):
        engine.store.profiles = []
        return {}, SimpleNamespace(is_live=True, flv_url="https://cdn.example/live.flv")

    with patch.object(engine, "_resolve", side_effect=resolving), patch.object(engine, "_start_recording") as start:
        engine._check_profile(profile)
    start.assert_not_called()
    assert not engine.store.profiles


def test_resume_start_failure_is_contained():
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/old"}
    engine = engine_for(profile)
    engine.recordings["p"] = {"session_dir": "mock-session", "recovery_started_at": time.time() - 1}
    stream = SimpleNamespace(is_live=True, flv_url="https://cdn.example/live.flv")
    with patch.object(engine, "_resolve", AsyncMock(return_value=({}, stream))), patch.object(
        engine, "_start_recording", side_effect=OSError("disk full")
    ):
        with patch.object(engine, "_save_recording_manifest"):
            engine._check_profile(profile)
        assert engine.recordings["p"]["recovery_started_at"] > 0


def test_explicit_empty_save_persists(tmp_path):
    target = tmp_path / "profiles.json"
    target.write_text('[{"id":"last","url":"https://live.douyin.com/123"}]', encoding="utf-8")
    app.save_json(target, [], allow_empty=True)
    assert json.loads(target.read_text(encoding="utf-8")) == []


def test_invalid_ffmpeg_is_rejected_before_creating_session_or_log(tmp_path):
    profile = {"id": "p", "output_dir": str(tmp_path)}
    engine = engine_for(profile)
    stream = SimpleNamespace(flv_url="https://example.test/live.flv")
    with patch.object(app, "resolve_ffmpeg_executable", side_effect=ValueError("invalid tool")), patch.object(
        engine, "_new_recording_session"
    ) as new_session, patch.object(app, "open") as open_file:
        with pytest.raises(ValueError, match="invalid tool"):
            engine._start_recording(profile, stream)
    new_session.assert_not_called()
    open_file.assert_not_called()


@pytest.mark.parametrize("change", ["edit", "delete", "stop"])
def test_media_cancellation_tracks_profile_ownership(change):
    profile = {"id": "p", "url": "https://www.douyin.com/user/old"}
    store = SimpleNamespace(get_profile=Mock(return_value=dict(profile)))
    stop = threading.Event()
    cancellation = app.ProfileCancellation(stop, store, profile)
    assert not cancellation.is_set()
    if change == "edit":
        store.get_profile.return_value["url"] = "https://www.douyin.com/user/new"
    elif change == "delete":
        store.get_profile.return_value = None
    else:
        stop.set()
    assert cancellation.is_set()
    assert cancellation.wait(1)


def test_download_propagates_shutdown(tmp_path):
    output = tmp_path / "test.jpg"
    body = b"\xff\xd8" + b"x" * 4096

    def cancelled(_progress):
        raise InterruptedError("Shutdown requested during media download")

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client:
        with pytest.raises(InterruptedError):
            media.download_bytes(client, "https://p.douyinpic.com/test.jpg", output, progress_callback=cancelled)
    assert not output.exists()


def test_download_blocks_unsafe_redirect(tmp_path):
    seen = []

    def server(request):
        seen.append(request.url.host)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private.jpg"})
        return httpx.Response(200, content=b"\xff\xd8" + b"x" * 4096)

    with httpx.Client(transport=httpx.MockTransport(server), follow_redirects=True) as client:
        with pytest.raises(ValueError):
            media.download_bytes(client, "https://p.douyinpic.com/test.jpg", tmp_path / "image.jpg")
    assert seen == ["p.douyinpic.com"]


def test_cdp_retains_coalesced_messages():
    first = json.dumps({"method": "Network.responseReceived"}).encode()
    second = json.dumps({"id": 7, "result": {}}).encode()
    session = media.CdpSession("ws://127.0.0.1/mock")
    session._sock = Mock()
    session._sock.recv.side_effect = socket.timeout()
    session._buffered = b"\x81" + bytes([len(first)]) + first + b"\x81" + bytes([len(second)]) + second
    assert session._recv_message()["method"] == "Network.responseReceived"
    assert session._recv_message() == {"id": 7, "result": {}}


def test_adoption_rejects_finalization_process(tmp_path):
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/123", "output_dir": str(tmp_path)}
    engine = engine_for(profile)
    output = tmp_path / "old.finalizing.mkv"
    output.write_bytes(b"test")
    command = f'ffmpeg.exe -f concat -safe 0 -i "{tmp_path / "parts.txt"}" -c copy "{output}"'
    with patch.object(engine, "_active_ffmpeg_processes", return_value=[{"ProcessId": 999999, "CommandLine": command}]), patch.object(app, "AdoptedProcess"):
        engine.adopt_existing_ffmpeg()
    assert "p" not in engine.processes


def test_adoption_preserves_url_refresh_timing(tmp_path):
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/123", "output_dir": str(tmp_path)}
    engine = engine_for(profile)
    output = tmp_path / ".recording_sessions" / "session" / "part-0001.mkv"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"test")
    recording = {"profile_id": "p", "parts": [str(output)], "rotate_at": 1, "segment_started_at": 1}
    manifest = engine._manifest_payload(recording)
    (output.parent / "session.json").write_text(json.dumps(manifest), encoding="utf-8")
    recovered = engine._recover_session_manifest("p", str(output))
    assert recovered["rotate_at"] == 1
    assert recovered["segment_started_at"] == 1


def test_adoption_handles_a_part_not_created_yet(tmp_path):
    engine = engine_for({"id": "p"})
    output = tmp_path / "part-0001.mkv"
    (tmp_path / "session.json").write_text(json.dumps({"profile_id": "p", "status": "recording"}))
    recovered = engine._recover_session_manifest("p", str(output))
    assert recovered["rotate_at"] > time.time()
    assert recovered["segment_started_at"] > 0


def test_check_mode_never_starts_recording():
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/123", "enabled": True}
    engine = engine_for(profile)
    stream = SimpleNamespace(is_live=True, flv_url="https://cdn.example/live.flv")
    with patch.object(app, "RecorderStore", return_value=engine.store), patch.object(app, "MonitorEngine", return_value=engine), patch.object(
        app, "setup_logging"
    ), patch.object(app, "install_exception_hooks"), patch.object(app, "print_console"), patch.object(
        app, "acquire_app_lock"
    ) as lock, patch.object(engine, "_resolve", AsyncMock(return_value=({}, stream))), patch.object(engine, "_start_recording") as start:
        app.run_check()
    start.assert_not_called()
    lock.assert_not_called()


def test_story_captcha_sets_shared_breaker():
    profile = {"id": "p", "name": "p", "auto_download_stories": True}
    store = SimpleNamespace(settings={}, get_profile=lambda _: profile)
    with patch.object(media, "load_json", return_value={}):
        engine = app.MediaDownloadEngine(store, queue.Queue())
    summary = {"videos": {"status": "disabled"}, "stories": {"status": "captcha"}}
    with patch.object(app, "download_profile", return_value=summary), patch.object(engine, "_save_circuit_breaker_state"):
        engine._check_profile(profile)
    assert engine.consecutive_failures["p"] == 1
    assert engine._last_video_status["p"] == "captcha"


def test_single_download_preserves_media_history(tmp_path):
    state_path = tmp_path / "douyin_media_state.json"
    state_path.write_text(json.dumps({"downloaded_video_ids": ["earlier-video"], "downloaded_story_ids": ["earlier-story"]}), encoding="utf-8")
    aweme = {"aweme_id": "123456789", "desc": "sample"}
    with patch.object(media, "_mobile_cookie_header", return_value=""), patch.object(
        media, "fetch_aweme_detail", return_value=aweme
    ), patch.object(media, "download_aweme_items", return_value=media.MediaResult(skipped=1)):
        media.download_video_by_url("https://www.douyin.com/video/123456789", str(tmp_path))
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "earlier-video" in saved["downloaded_video_ids"]
    assert "earlier-story" in saved["downloaded_story_ids"]


def test_unreadable_profiles_raise_without_empty_fallback(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text('[{"id":"retained"}]', encoding="utf-8")
    with patch("builtins.open", side_effect=PermissionError("temporary lock")):
        with pytest.raises(PermissionError):
            app.load_json(path, [])


def test_resolving_live_does_not_clear_failure_count():
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/123"}
    engine = engine_for(profile)
    stream = SimpleNamespace(is_live=True, flv_url="https://cdn.example/live.flv")
    with patch.object(engine, "_resolve", AsyncMock(return_value=({}, stream))), patch.object(engine, "_start_recording"):
        for _ in range(3):
            # _poll_processes adds one error after a short-lived FFmpeg exit.
            engine.error_counts["p"] = engine.error_counts.get("p", 0) + 1
            engine._check_profile(profile)
            assert engine.error_counts["p"] == _ + 1


def test_store_last_deletion_persists_and_failed_save_rolls_back(tmp_path):
    target = tmp_path / "profiles.json"
    target.write_text('[{"id":"p"}]', encoding="utf-8")
    store = object.__new__(app.RecorderStore)
    store.lock = threading.RLock()
    store.profiles = [{"id": "p"}]
    with patch.object(app, "PROFILES_FILE", target), patch.object(app, "save_json", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            store.remove_profile("p")
    assert store.profiles == [{"id": "p"}]
    with patch.object(app, "PROFILES_FILE", target):
        store.remove_profile("p")
    assert store.profiles == json.loads(target.read_text()) == []


def test_store_read_failure_never_saves_defaults(tmp_path):
    settings = tmp_path / "settings.json"
    profiles = tmp_path / "profiles.json"
    settings.write_text(json.dumps(app.default_settings()), encoding="utf-8")
    profiles.write_text('[{"id":"p"}]', encoding="utf-8")
    original_open = open

    def locked(path, *args, **kwargs):
        if Path(path) == profiles:
            raise PermissionError("temporarily unreadable")
        return original_open(path, *args, **kwargs)

    with patch.object(app, "SETTINGS_FILE", settings), patch.object(app, "PROFILES_FILE", profiles), patch(
        "builtins.open", side_effect=locked
    ), patch.object(app, "save_json") as save:
        with pytest.raises(PermissionError):
            app.RecorderStore()
    save.assert_not_called()
    assert json.loads(profiles.read_text()) == [{"id": "p"}]


def test_active_url_change_stops_old_session_and_schedules_new_probe():
    old = {"id": "p", "url": "https://live.douyin.com/old"}
    current = {**old, "url": "https://live.douyin.com/new"}
    engine = engine_for(current)
    with patch.object(engine, "stop_profile_recording") as stop:
        engine.profile_changed("p", old)
    stop.assert_called_once()
    assert engine.next_check["p"] == 0


def test_recovery_age_is_not_reset_by_failed_starts():
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/123"}
    engine = engine_for(profile)
    started = time.time() - 1801
    engine.recordings["p"] = {"session_dir": "mock-session", "recovery_started_at": started}
    stream = SimpleNamespace(is_live=True, flv_url="https://cdn.example/live.flv")
    with patch.object(engine, "_resolve", AsyncMock(return_value=({}, stream))), patch.object(
        engine, "_start_recording", side_effect=OSError("start failed")
    ), patch.object(engine, "_finalize_recording_session") as finalize:
        engine._check_profile(profile)
    finalize.assert_called_once()


def test_monitor_keeps_checking_after_error_handler_fails():
    first = {"id": "a", "name": "a", "url": "https://live.douyin.com/a"}
    second = {"id": "b", "name": "b", "url": "https://live.douyin.com/b"}
    engine = engine_for(first)
    engine.store.profiles.append(second)
    seen = []

    def check(profile):
        seen.append(profile["id"])
        if profile["id"] == "a":
            raise OSError("manifest save failed")
        engine.stop_event.set()

    with patch.object(engine, "_check_profile", side_effect=check), patch.object(engine, "_poll_processes"):
        engine._run()
    assert seen == ["a", "b"]


def test_cdp_retains_fragments_across_timeout():
    session = media.CdpSession("ws://127.0.0.1/mock")
    session._sock = Mock()
    session._sock.recv.side_effect = socket.timeout()
    data = b'{"id":8,"result":{}}'
    first, rest = data[:5], data[5:]
    session._buffered = b"\x01" + bytes([len(first)]) + first
    with pytest.raises(TimeoutError):
        session._recv_message(timeout=0.01)
    session._buffered = b"\x80" + bytes([len(rest)]) + rest
    assert session._recv_message(timeout=0.1) == {"id": 8, "result": {}}


def test_allowed_redirect_and_validation_preserve_previous_file(tmp_path):
    output = tmp_path / "image.jpg"
    original = b"\xff\xd8" + b"x" * 4096
    output.write_bytes(original)
    seen = []

    def server(request):
        seen.append(request.url.host)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://other.douyinpic.com/b.jpg"})
        return httpx.Response(200, content=b"broken")

    with httpx.Client(transport=httpx.MockTransport(server), follow_redirects=True) as client:
        with pytest.raises(ValueError):
            media.download_bytes(client, "https://p.douyinpic.com/a.jpg", output)
    assert seen == ["p.douyinpic.com", "other.douyinpic.com"]
    assert output.read_bytes() == original
    assert not list(tmp_path.glob("*.part.*"))


def test_cancelled_profile_does_not_retry_browser_or_mobile(tmp_path):
    signal = threading.Event()
    profile = {"id": "p", "name": "p", "output_dir": str(tmp_path), "url": "https://www.douyin.com/user/example"}

    def cancel(*_args, **_kwargs):
        signal.set()
        media.check_cancelled()

    with patch.object(media, "_mobile_cookie_header", return_value=""), patch.object(
        media, "fetch_posts", side_effect=cancel
    ), patch.object(media, "fetch_posts_via_browser") as browser:
        with pytest.raises(InterruptedError):
            media.download_profile(profile, cancel_event=signal)
    browser.assert_not_called()


def test_cancelled_browser_lease_still_closes_its_browser():
    signal = threading.Event()
    with patch.object(media, "ensure_media_fetch_browser", return_value={"cdp_url": "http://127.0.0.1:9344"}), patch.object(
        media, "cdp_is_available", return_value=True
    ), patch.object(media, "close_cdp_browser") as close:
        with media.cancellation_scope(signal), media.media_fetch_browser():
            signal.set()
    close.assert_called_once()


def test_legacy_adoption_reconstructs_refresh_deadline(tmp_path):
    output = tmp_path / "part-0001.mkv"
    output.write_bytes(b"test")
    (tmp_path / "session.json").write_text(json.dumps({"profile_id": "p", "parts": [str(output)]}), encoding="utf-8")
    engine = engine_for({"id": "p"})
    now = time.time()
    recovered = engine._recover_session_manifest("p", str(output))
    assert now < recovered["rotate_at"] <= time.time() + engine.setting_seconds("recording_segment_max_seconds")


def test_rapid_ffmpeg_deaths_accumulate_and_delay_next_start():
    profile = {"id": "p", "name": "p", "url": "https://live.douyin.com/p"}
    engine = engine_for(profile)
    delays = []
    with patch.object(engine, "_save_recording_manifest"):
        for index in range(3):
            process = Mock(returncode=1)
            process.poll.return_value = 1
            engine.processes["p"] = process
            engine.recordings["p"] = {"session_dir": "mock", "parts": [], "segment_started_at": time.time()}
            engine._poll_processes()
            delays.append(engine.next_check["p"] - time.monotonic())
            assert engine.recording_error_counts["p"] == index + 1
    assert 0 < delays[0] < delays[1] < delays[2]


def test_cli_ffmpeg_stderr_cannot_fill_an_unread_pipe(tmp_path):
    import douyin_live_watcher as watcher
    real_popen = watcher.subprocess.Popen
    stream = SimpleNamespace(anchor_name="Fixture", title="Warnings", flv_url="https://cdn.example/live.flv")
    config = {"output_dir": str(tmp_path), "container": "mkv", "ffmpeg_path": "ffmpeg.exe"}

    def noisy_process(_cmd, **kwargs):
        assert kwargs["stderr"] != watcher.subprocess.PIPE
        # A real child writes substantially more than a normal pipe can buffer.
        child = real_popen([sys.executable, "-c", "import sys; sys.stderr.write('warning' * 200000)"], **kwargs)
        try:
            child.wait(timeout=5)
        except BaseException:
            child.kill()
            child.wait(timeout=5)
            raise
        return child

    with patch.object(watcher, "resolve_trusted_executable", return_value="ffmpeg.exe"), patch.object(
        watcher.subprocess, "Popen", side_effect=noisy_process
    ):
        watcher.run_ffmpeg(config, stream, Mock())
    assert sum(p.stat().st_size for p in (tmp_path / "logs").glob("*.log")) >= 1400000
