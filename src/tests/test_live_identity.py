import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import douyin_recorder_app as app


def test_dialog_resolves_follow_live_link():
    dialog = SimpleNamespace(store=SimpleNamespace(settings={"quality": "OD"}))
    with patch.object(app, "DouyinLiveStream") as factory:
        live = factory.return_value
        live.fetch_web_stream_data = AsyncMock(return_value={})
        live.fetch_app_stream_data = AsyncMock()
        live.fetch_stream_url = AsyncMock()
        asyncio.run(app.ProfileDialog.resolve_room(dialog, "https://www.douyin.com/follow/live/123?anchor_id=456"))
    live.fetch_web_stream_data.assert_awaited_once_with("https://live.douyin.com/123")


def test_verified_cache_is_reused_only_for_same_source():
    engine = app.MonitorEngine(SimpleNamespace(settings={"quality": "OD"}), queue.Queue())
    profile = {"url": "https://www.douyin.com/user/owner", "fallback_live_url": "https://live.douyin.com/owner"}
    profile["fallback_source_url"] = profile["url"]
    with patch.object(app, "DouyinLiveStream") as factory:
        live = factory.return_value
        live.fetch_web_stream_data = AsyncMock(return_value={})
        live.fetch_stream_url = AsyncMock()
        asyncio.run(engine._resolve_douyin(profile))
    live.fetch_web_stream_data.assert_awaited_once_with(profile["fallback_live_url"])


def test_url_normalization_checks_hostname():
    assert app.canonical_douyin_live_url("https://example.com/follow/live/123") == ""
    assert app.canonical_douyin_live_url("https://example.com/?url=https://live.douyin.com/123") == ""
    assert app.canonical_douyin_live_url("https://live.douyin.com/name?from=share") == "https://live.douyin.com/name"


def test_follow_live_link_overrides_stale_fallback():
    engine = app.MonitorEngine(SimpleNamespace(settings={"quality": "OD"}), queue.Queue())
    profile = {
        "url": "https://www.douyin.com/follow/live/167551967373?anchor_id=59245200441",
        "fallback_live_url": "https://live.douyin.com/ChenPiTu",
    }
    with patch.object(app, "DouyinLiveStream") as factory:
        live = factory.return_value
        live.fetch_web_stream_data = AsyncMock(return_value={"anchor_name": "correct"})
        live.fetch_app_stream_data = AsyncMock()
        live.fetch_stream_url = AsyncMock()
        asyncio.run(engine._resolve_douyin(profile))
    live.fetch_web_stream_data.assert_awaited_once_with("https://live.douyin.com/167551967373")
    live.fetch_app_stream_data.assert_not_awaited()


def test_profile_url_does_not_trust_unbound_cached_room():
    engine = app.MonitorEngine(SimpleNamespace(settings={"quality": "OD"}), queue.Queue())
    profile = {"url": "https://www.douyin.com/user/new-owner", "fallback_live_url": "https://live.douyin.com/old-owner"}
    with patch.object(app, "DouyinLiveStream") as factory:
        live = factory.return_value
        live.fetch_web_stream_data = AsyncMock()
        live.fetch_app_stream_data = AsyncMock(return_value={})
        live.fetch_stream_url = AsyncMock()
        asyncio.run(engine._resolve_douyin(profile))
    live.fetch_app_stream_data.assert_awaited_once_with(profile["url"])
    live.fetch_web_stream_data.assert_not_awaited()
