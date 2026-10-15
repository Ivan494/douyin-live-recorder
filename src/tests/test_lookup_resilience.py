"""Offline fault injection for account lookup retries and admission control."""

import json
import logging
import socket
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import douyin_media_downloader as media
import douyin_recorder_app as app
from lookup_resilience import (
    LOOKUP_DEFAULTS, AccountCircuitBreaker, LookupDeferred, LookupPolicy,
    account_key, retry_lookup,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Network access is forbidden in lookup tests")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(media, "_mobile_cookie_header", lambda: "")
    monkeypatch.setattr(media, "fetch_posts_via_browser", forbidden)
    monkeypatch.setattr(app.MediaDownloadEngine, "_load_circuit_breaker_state", lambda _self: None)
    monkeypatch.setattr(app.MediaDownloadEngine, "_save_circuit_breaker_state", lambda _self: None)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    clock = Mock(return_value=100.0)
    breaker = AccountCircuitBreaker(clock=clock)
    monkeypatch.setattr(media, "_lookup_circuits", breaker)
    waits = Mock()
    monkeypatch.setattr(media, "_cancel_wait", waits)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(media.httpx, "Client", Mock(return_value=client))
    profile = {"id": "account-a", "name": "Account A", "url": "https://live.douyin.com/123",
               "output_dir": str(tmp_path / "account-a"), "auto_download_stories": True}
    settings = {**LOOKUP_DEFAULTS, "media_lookup_resilience_enabled": True,
                "media_lookup_failure_threshold": 2, "media_lookup_cooldown_seconds": 60}
    return SimpleNamespace(clock=clock, breaker=breaker, waits=waits, client=client,
                           profile=profile, settings=settings)


def policy(**overrides):
    return LookupPolicy.from_settings({**LOOKUP_DEFAULTS,
                                      "media_lookup_resilience_enabled": True, **overrides})


def run_retry(operation, selected_policy, **overrides):
    options = dict(retryable=media._retryable_lookup_error, wait=Mock(),
                   check_cancelled=Mock(), uniform=lambda _lo, hi: hi)
    options.update(overrides)
    return retry_lookup(operation, selected_policy, **options)


@pytest.mark.parametrize("flag", [None, False, "false", "true", 1, 0])
def test_default_off_and_strict_opt_in(flag):
    assert LookupPolicy.from_settings({"media_lookup_resilience_enabled": flag}) is None
    operation = Mock(side_effect=TimeoutError("offline"))
    with pytest.raises(TimeoutError):
        run_retry(operation, None)
    operation.assert_called_once()
    assert app.default_settings()["media_lookup_resilience_enabled"] is False
    shipped = json.loads((Path(media.__file__).parent / "settings.json").read_text())
    assert shipped["media_lookup_resilience_enabled"] is False


def test_policy_validation_and_hard_limits():
    parsed = policy(media_lookup_max_attempts=999, media_lookup_failure_threshold=-1,
                    media_lookup_backoff_base_seconds="nan",
                    media_lookup_backoff_max_seconds=0, media_lookup_cooldown_seconds="inf")
    assert parsed.max_attempts == 5
    assert parsed.failure_threshold == 1
    assert parsed.base_seconds == parsed.max_seconds == 0.1
    assert parsed.cooldown_seconds == 900
    assert policy(media_lookup_max_attempts=None).max_attempts == 3
    assert policy(media_lookup_max_attempts=True).max_attempts == 3


def test_shipped_policy_matches_runtime_defaults():
    shipped = json.loads((Path(media.__file__).parent / "settings.json").read_text())
    for key, value in LOOKUP_DEFAULTS.items():
        assert shipped[key] == app.default_settings()[key] == value


@pytest.mark.parametrize("fraction", [0, 0.3, 1])
def test_retry_bound_and_jittered_exponential_cap(fraction):
    operation = Mock(side_effect=TimeoutError("offline"))
    waits = Mock()
    p = replace(policy(), max_attempts=5, base_seconds=2, max_seconds=5)
    with pytest.raises(TimeoutError):
        run_retry(operation, p, wait=waits, uniform=lambda lo, hi: lo + fraction * (hi - lo))
    assert operation.call_count == 5
    assert [call.args[0] for call in waits.call_args_list] == [
        cap * (0.5 + 0.5 * fraction) for cap in (2, 4, 5, 5)]


def test_transient_failure_recovers_without_extra_attempt_or_delay():
    operation = Mock(side_effect=[TimeoutError(), "recovered"])
    waits = Mock()
    assert run_retry(operation, policy(), wait=waits) == "recovered"
    assert operation.call_count == 2
    waits.assert_called_once_with(1.0)


@pytest.mark.parametrize("error", [media.LoginRequiredError("expired"),
                                  media.CaptchaDetectedError("verify"),
                                  Exception("it triggered risk control"),
                                  media.EmptyApiResponseError("mobile_post_api: no session cookies"),
                                  media.EmptyApiResponseError("mobile_post_api: signer module not available"),
                                  media.EmptyApiResponseError("mobile_post_api: no posts returned"),
                                  httpx.UnsupportedProtocol("bad URL"),
                                  httpx.LocalProtocolError("bad headers"),
                                  ValueError("bad configuration"),
                                  InterruptedError("cancelled")])
def test_terminal_errors_are_not_immediately_retried(error):
    operation = Mock(side_effect=error)
    waits = Mock()
    with pytest.raises(type(error)):
        run_retry(operation, policy(), wait=waits)
    operation.assert_called_once()
    waits.assert_not_called()


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (403, False),
                                             (404, False), (408, True), (429, False),
                                             (500, True), (502, True), (503, True), (504, True)])
def test_http_error_classification(status, retryable):
    request = httpx.Request("GET", "https://www.douyin.com/offline")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("offline", request=request, response=response)
    assert media._retryable_lookup_error(error) is retryable


@pytest.mark.parametrize("error", [httpx.ReadTimeout("offline"), httpx.ConnectError("offline"),
                                  httpx.ReadError("offline"), httpx.RemoteProtocolError("offline"),
                                  media.EmptyApiResponseError("offline empty body"),
                                  RuntimeError("Could not resolve sec_user_id for this Douyin profile")])
def test_transient_lookup_errors_are_retried(error):
    operation = Mock(side_effect=[error, "recovered"])
    assert run_retry(operation, policy()) == "recovered"
    assert operation.call_count == 2


def test_cancellation_during_wait_stops_before_next_request():
    operation = Mock(side_effect=TimeoutError())
    cancelled = threading.Event()

    def check():
        if cancelled.is_set():
            raise InterruptedError()

    with pytest.raises(InterruptedError):
        run_retry(operation, policy(), wait=lambda _seconds: cancelled.set(), check_cancelled=check)
    operation.assert_called_once()


def test_circuit_opens_at_threshold_and_only_one_half_open_probe(sandbox):
    p = LookupPolicy.from_settings(sandbox.settings)
    breaker = sandbox.breaker
    for _ in range(2):
        state, probe = breaker.acquire("a", p)
        assert not probe
        breaker.finish("a", state, p, failed=True)
    with pytest.raises(LookupDeferred) as raised:
        breaker.acquire("a", p)
    assert raised.value.retry_after == 60
    sandbox.clock.return_value = 159.99
    with pytest.raises(LookupDeferred):
        breaker.acquire("a", p)
    sandbox.clock.return_value = 160
    state, probe = breaker.acquire("a", p)
    assert probe
    with pytest.raises(LookupDeferred, match="busy"):
        breaker.acquire("a", p)
    # A half-open failure starts a fresh cooldown.
    breaker.finish("a", state, p, failed=True)
    assert breaker.remaining("a", p) == 60
    sandbox.clock.return_value = 220
    state, probe = breaker.acquire("a", p)
    assert probe
    breaker.finish("a", state, p, failed=False)
    assert breaker.remaining("a", p) == 0
    state, probe = breaker.acquire("a", p)
    assert not probe and state.failures == 0


def test_success_resets_streak_and_other_accounts_remain_available(sandbox):
    p = LookupPolicy.from_settings(sandbox.settings)
    breaker = sandbox.breaker
    state, _ = breaker.acquire("a", p)
    breaker.finish("a", state, p, failed=True)
    state, _ = breaker.acquire("a", p)
    breaker.finish("a", state, p, failed=False)
    state, _ = breaker.acquire("a", p)
    breaker.finish("a", state, p, failed=True)
    assert breaker.remaining("a", p) == 0
    state, _ = breaker.acquire("a", p)
    breaker.finish("a", state, p, failed=True)
    other, probe = breaker.acquire("b", p)
    assert not probe
    breaker.finish("b", other, p, failed=False)


def test_concurrent_half_open_admission_is_atomic(sandbox):
    p = replace(LookupPolicy.from_settings(sandbox.settings), failure_threshold=1)
    breaker = sandbox.breaker
    state, _ = breaker.acquire("a", p)
    breaker.finish("a", state, p, failed=True)
    sandbox.clock.return_value = 160
    start = threading.Barrier(9)
    admitted, deferred = [], []

    def contender():
        start.wait(timeout=3)
        try:
            admitted.append(breaker.acquire("a", p))
        except LookupDeferred:
            deferred.append(True)

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len(admitted) == 1 and len(deferred) == 7
    breaker.finish("a", admitted[0][0], p, failed=False)


def test_identity_retries_before_media_writes_and_resets_after_recovery(sandbox, monkeypatch):
    calls = [TimeoutError("offline"), {"sec_user_id": "sec-a"}]

    async def identity(_profile):
        value = calls.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(media, "resolve_profile_identity", identity)
    scan = Mock(return_value=([], "offline empty pack", True))
    monkeypatch.setattr(media, "fetch_stories", scan)
    download = Mock(return_value=media.MediaResult(status="ok"))
    monkeypatch.setattr(media, "download_aweme_items", download)
    result = media.download_profile(sandbox.profile, sandbox.settings, videos=False, stories=True)
    assert result["stories"]["status"] == "no_active_stories"
    assert not calls
    sandbox.waits.assert_called_once()
    scan.assert_called_once()
    download.assert_called_once()
    assert account_key(sandbox.profile) not in sandbox.breaker._states


def test_identity_exhaustion_opens_circuit_before_any_io_on_later_checks(sandbox, monkeypatch):
    resolver = Mock(side_effect=TimeoutError("offline"))
    monkeypatch.setattr(media, "resolve_profile_identity_sync", resolver)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            media.download_profile(sandbox.profile, sandbox.settings)
    assert resolver.call_count == 6  # Three attempts, one failure per check.
    resolver.reset_mock()
    moved = {**sandbox.profile, "output_dir": str(Path(sandbox.profile["output_dir"]).parent / "untouched")}
    with pytest.raises(LookupDeferred):
        media.download_profile(moved, sandbox.settings)
    assert not Path(moved["output_dir"]).exists()
    resolver.assert_not_called()
    sandbox.clock.return_value = 160
    with pytest.raises(TimeoutError):
        media.download_profile(sandbox.profile, sandbox.settings)
    resolver.assert_called_once()  # Half-open probe does not retry immediately.
    with pytest.raises(LookupDeferred):
        media.download_profile(sandbox.profile, sandbox.settings)


def test_profile_without_id_uses_query_free_stable_key():
    assert account_key({"url": "https://live.douyin.com/123?token=one"}) == account_key(
        {"url": "https://live.douyin.com/123?token=two", "cookies": "dummy"})


@pytest.mark.parametrize("status", ["api_empty", "error", "captcha", "login_required",
                                    "partial", "unavailable", "blocked"])
def test_returned_failure_statuses_count_toward_account_breaker(sandbox, monkeypatch, status):
    monkeypatch.setattr(media, "_download_profile", Mock(return_value={"stories": {"status": status}}))
    for _ in range(2):
        media.download_profile(sandbox.profile, sandbox.settings)
    with pytest.raises(LookupDeferred):
        media.download_profile(sandbox.profile, sandbox.settings)


def test_cancellation_is_neutral_and_releases_admission(sandbox, monkeypatch):
    download = Mock(side_effect=InterruptedError("cancelled"))
    monkeypatch.setattr(media, "_download_profile", download)
    for _ in range(3):
        with pytest.raises(InterruptedError):
            media.download_profile(sandbox.profile, sandbox.settings)
    state = sandbox.breaker._states[account_key(sandbox.profile)]
    assert state.failures == 0 and not state.in_flight
    assert getattr(media._cancellation, "lookup_policy", None) is None


def test_half_open_cancellation_allows_another_probe_without_resetting_streak(sandbox, monkeypatch):
    download = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(media, "_download_profile", download)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            media.download_profile(sandbox.profile, sandbox.settings)
    sandbox.clock.return_value = 160
    download.side_effect = InterruptedError()
    with pytest.raises(InterruptedError):
        media.download_profile(sandbox.profile, sandbox.settings)
    state = sandbox.breaker._states[account_key(sandbox.profile)]
    assert state.failures == 2 and state.opened_at == 100 and not state.in_flight
    download.side_effect = None
    download.return_value = {"stories": {"status": "no_active_stories"}}
    media.download_profile(sandbox.profile, sandbox.settings)
    assert account_key(sandbox.profile) not in sandbox.breaker._states


def test_disabling_flag_bypasses_existing_open_circuit(sandbox, monkeypatch):
    download = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(media, "_download_profile", download)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            media.download_profile(sandbox.profile, sandbox.settings)
    download.side_effect = None
    download.return_value = {"stories": {"status": "ok"}}
    settings = {**sandbox.settings, "media_lookup_resilience_enabled": False}
    assert media.download_profile(sandbox.profile, settings) == download.return_value
    assert media.profile_lookup_cooldown(sandbox.profile, settings) == 0


@pytest.mark.parametrize("kind", ["posts", "web_posts", "browser_posts", "stories"])
def test_metadata_retry_does_not_repeat_downloads(sandbox, monkeypatch, kind):
    profile = {**sandbox.profile, "original_profile_url": "https://www.douyin.com/user/sec-a",
               "cookies": "dummy" if kind == "posts" else ""}
    monkeypatch.setattr(media, "_http_fastpath_failures", {})
    monkeypatch.setattr(media, "resolve_profile_identity_sync", Mock(side_effect=AssertionError("quick ID")))
    posts = [{"aweme_id": "fixture"}]
    result = (posts, "offline pack", True) if kind == "stories" else posts
    scan = Mock(side_effect=[TimeoutError("offline"), result])
    source = {"posts": "fetch_posts_via_mobile_api", "web_posts": "fetch_posts",
              "browser_posts": "fetch_posts_via_browser", "stories": "fetch_stories"}[kind]
    monkeypatch.setattr(media, source, scan)
    if kind == "browser_posts":
        monkeypatch.setattr(media, "fetch_posts", Mock(return_value=[]))
    download = Mock(return_value=media.MediaResult(status="ok", downloaded=1))
    monkeypatch.setattr(media, "download_aweme_items", download)
    summary = media.download_profile(profile, sandbox.settings, videos=kind != "stories", stories=kind == "stories")
    assert scan.call_count == 2
    download.assert_called_once()
    assert summary["stories" if kind == "stories" else "videos"]["status"] == "ok"


def test_live_identity_fallback_retries_and_keeps_probes_cookieless(sandbox, monkeypatch):
    stream = Mock()
    stream.fetch_web_stream_data = AsyncMock(side_effect=[TimeoutError("offline"), {
        "data": {"user": {"sec_uid": "sec-a", "id_str": "123", "nickname": "Account A"}}}])
    factory = Mock(return_value=stream)
    monkeypatch.setattr(media, "DouyinLiveStream", factory)
    monkeypatch.setattr(media, "fetch_stories", Mock(return_value=([], "empty pack", True)))
    monkeypatch.setattr(media, "download_aweme_items", Mock(return_value=media.MediaResult(status="ok")))
    summary = media.download_profile({**sandbox.profile, "cookies": "dummy"}, sandbox.settings,
                                     videos=False, stories=True)
    assert summary["sec_user_id"] == "sec-a"
    assert stream.fetch_web_stream_data.await_count == 2
    assert all(call.kwargs["cookies"] is None for call in factory.call_args_list)


def test_asset_timeout_does_not_retry_metadata_or_download(sandbox, monkeypatch):
    profile = {**sandbox.profile, "original_profile_url": "https://www.douyin.com/user/sec-a"}
    scan = Mock(return_value=([{"aweme_id": "fixture"}], "pack", True))
    monkeypatch.setattr(media, "fetch_stories", scan)
    download = Mock(side_effect=TimeoutError("offline asset failure"))
    monkeypatch.setattr(media, "download_aweme_items", download)
    summary = media.download_profile(profile, sandbox.settings, videos=False, stories=True)
    assert summary["stories"]["status"] == "error"
    scan.assert_called_once()
    download.assert_called_once()
    sandbox.waits.assert_not_called()


def test_engine_deferral_does_not_log_exception_or_increment_failures(sandbox, monkeypatch, caplog):
    store = SimpleNamespace(settings=sandbox.settings, get_profile=lambda _id: sandbox.profile)
    engine = app.MediaDownloadEngine(store, app.RecorderEventQueue(), notify_callback=Mock())
    monkeypatch.setattr(app, "download_profile", Mock(side_effect=LookupDeferred(45)))
    with caplog.at_level(logging.WARNING):
        engine._check_profile(sandbox.profile)
    assert not caplog.records
    assert not engine.consecutive_failures
    engine.notify_callback.assert_not_called()
    event = engine.event_queue.get_nowait()
    assert event["state"]["media_status"] == "Lookup cooling down"
    assert engine.next_check[sandbox.profile["id"]] > 0


@pytest.mark.parametrize("enabled,expected_checks", [(True, ["account-a", "account-b"]),
                                                   (False, ["account-a"])])
def test_scheduler_isolates_captcha_and_honors_cooldown_only_when_enabled(
        sandbox, monkeypatch, enabled, expected_checks):
    profiles = [sandbox.profile, {**sandbox.profile, "id": "account-b"}]
    settings = {**sandbox.settings, "media_lookup_resilience_enabled": enabled}
    store = SimpleNamespace(settings=settings, profiles=profiles)
    engine = app.MediaDownloadEngine(store, app.RecorderEventQueue())
    engine._last_video_status["account-a"] = "captcha"
    engine.consecutive_failures["account-a"] = 2
    checks = []
    monkeypatch.setattr(engine, "_check_profile", lambda profile: checks.append(profile["id"]))
    monkeypatch.setattr(app.random, "shuffle", lambda _profiles: None)
    monkeypatch.setattr(app.time, "monotonic", lambda: 100)
    monkeypatch.setattr(app, "profile_lookup_cooldown", lambda profile, _settings: 900
                        if profile["id"] == "account-a" else 0)
    # Stop after one cycle without sleeping or starting an engine thread.
    monkeypatch.setattr(engine.wake_event, "wait", lambda seconds: engine.stop_event.set()
                        if seconds == 1 else False)
    engine._run()
    assert checks == expected_checks
    if enabled:
        assert engine.next_check["account-a"] == 1000
        assert engine.next_check["account-b"] == 400


def test_enabled_engine_reports_one_sanitized_warning(sandbox, monkeypatch, caplog):
    store = SimpleNamespace(settings=sandbox.settings, get_profile=lambda _id: sandbox.profile)
    engine = app.MediaDownloadEngine(store, app.RecorderEventQueue())
    monkeypatch.setattr(app, "download_profile", Mock(side_effect=TimeoutError("dummy-secret-marker")))
    with caplog.at_level(logging.WARNING):
        engine._check_profile(sandbox.profile)
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    assert "TimeoutError" in caplog.text
    assert "dummy-secret-marker" not in caplog.text


def test_retry_and_transition_logs_do_not_expose_exception_or_account_secrets(sandbox, monkeypatch, caplog):
    secret = "dummy-secret-marker"
    profile = {**sandbox.profile, "name": secret, "cookies": secret, "id": secret}
    monkeypatch.setattr(media, "resolve_profile_identity_sync", Mock(side_effect=TimeoutError(secret)))
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            with pytest.raises(TimeoutError):
                media.download_profile(profile, sandbox.settings)
        for _ in range(5):
            with pytest.raises(LookupDeferred):
                media.download_profile(profile, sandbox.settings)
    assert secret not in caplog.text
    assert len([record for record in caplog.records if "circuit open" in record.message]) == 1
