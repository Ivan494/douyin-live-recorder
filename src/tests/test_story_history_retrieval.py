"""Incomplete author responses must not hide other visible story sources."""
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import httpx
import pytest

import douyin_media_downloader as media


AUTHOR = "target-sec"
UID = "1234567890"


def story(item_id, *, author=AUTHOR):
    item = {"aweme_id": item_id, "is_25_story": 1}
    if author is not None:
        item["author"] = {"sec_uid": author}
    return item


@contextmanager
def mobile_requests(handler):
    calls = []

    def request(_client, method, path, extra, *_args, **kwargs):
        calls.append((path, dict(extra), kwargs.get("host")))
        payload = handler(path, extra, kwargs.get("host"))
        return httpx.Response(200, json=payload,
                              request=httpx.Request(method, "https://example.test" + path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(media, "_check_mobile_signer", return_value=True))
        stack.enter_context(patch.object(media, "_persistent_mobile_device", return_value=("1", "2")))
        stack.enter_context(patch.object(media, "_mobile_device_profile", return_value={"own_uid": "551"}))
        stack.enter_context(patch.object(media, "_mobile_signed_request", side_effect=request))
        stack.enter_context(patch.object(media, "_cancel_wait"))
        yield calls


def fetch(*, scan_state=None):
    return media.fetch_stories_via_mobile_story_feed(
        object(), AUTHOR, user_id=UID, cookie_header="sessionid=test", scan_state=scan_state)


def empty_profile():
    return {"status_code": 0, "active_data": {"data": None, "has_more": False},
            "has_more": False, "next_offset": 0, "month_list": []}


def test_partial_author_failure_does_not_hide_target_tray():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return empty_profile()
        if "to_uid" in extra:
            raise httpx.ConnectError("history unavailable")
        return {"status_code": 0, "data": [story("tray-story")]}

    with mobile_requests(response):
        result = fetch()
        items, source = result
    assert [item["aweme_id"] for item in items] == ["tray-story"]
    assert "partial author results" in source
    assert "history unavailable" in source
    assert not result.complete


def test_partial_author_failure_does_not_hide_available_life_feed():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return empty_profile()
        if "to_uid" in extra:
            raise httpx.ConnectError("history unavailable")
        return {"status_code": 0, "data": []}

    with mobile_requests(response), patch.object(
        media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")
    ), patch.object(
        media, "fetch_stories_via_mobile_life_feed", return_value=([story("life-story")], "life/feed")
    ) as life:
        items, source, supported = media.fetch_stories(
            object(), {"cookies": "sessionid=test"}, AUTHOR, user_id=UID)
    assert [item["aweme_id"] for item in items] == ["life-story"]
    assert "history unavailable" in source
    assert supported
    life.assert_called_once()


def test_partial_nonempty_author_result_keeps_life_fallback_and_merges_items():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return {"status_code": 0, "active_data": {"data": [story("active")]},
                    "has_more": False}
        if "to_uid" in extra:
            raise httpx.ConnectError("history unavailable")
        return {"status_code": 0, "data": []}

    with mobile_requests(response), patch.object(
        media, "fetch_stories_via_mobile_post_api"
    ) as post, patch.object(
        media, "fetch_stories_via_mobile_life_feed",
        return_value=([story("active"), story("life-story")], "life/feed")
    ) as life:
        items, source, supported = media.fetch_stories(
            object(), {"cookies": "sessionid=test"}, AUTHOR, user_id=UID)
    assert [item["aweme_id"] for item in items] == ["active", "life-story"]
    assert "history unavailable" in source
    assert supported
    life.assert_called_once()
    post.assert_not_called()


def test_both_author_endpoints_must_finish_before_empty_is_authoritative():
    def response(path, extra, _host):
        assert "to_uid" in extra
        return empty_profile() if path == media.STORY_PROFILE_LIST_PATH else {
            "status_code": 0, "data": None, "has_more": False}

    with mobile_requests(response) as calls:
        result = fetch()
        items, source = result
    assert items == []
    assert "author endpoints complete" in source
    assert "empty pack" in source
    assert len(calls) == 2
    assert result.complete


def test_profile_offset_pages_merge_active_and_archive_without_duplicates():
    def response(path, extra, _host):
        if path == media.STORY_FEED_PATH:
            return {"status_code": 0, "data": [story("archive-2")], "has_more": False}
        if extra["offset"] == "0":
            return {"status_code": 0, "active_data": {"data": [story("active")]},
                    "data": [story("archive-1")], "has_more": True, "next_offset": 20}
        assert extra["offset"] == "20"
        return {"status_code": 0, "data": [story("archive-1"), story("archive-2")],
                "has_more": False, "next_offset": 40}

    with mobile_requests(response) as calls:
        items, source = fetch()
    assert [item["aweme_id"] for item in items] == ["archive-1", "active", "archive-2"]
    assert [extra["offset"] for path, extra, _ in calls
            if path == media.STORY_PROFILE_LIST_PATH] == ["0", "20"]
    assert "partial author results" not in source


def test_unverified_history_timestamp_does_not_get_guessed_as_offset():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return empty_profile()
        if "to_uid" in extra:
            assert extra["offset"] == "0"
            return {"status_code": 0, "data": [story("first-history")], "has_more": True,
                    "max_timestamp": 12345, "min_timestamp": 12300}
        return {"status_code": 0, "data": []}

    with mobile_requests(response) as calls:
        items, source = fetch()
    assert [item["aweme_id"] for item in items] == ["first-history"]
    assert "partial author results" in source
    assert "pagination cursor" in source
    assert len([call for call in calls if "to_uid" in call[1]
                and call[0] == media.STORY_FEED_PATH]) == 1


def test_author_cursor_cycle_stops_without_declaring_empty():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            offset = extra["offset"]
            return {"status_code": 0, "data": [], "has_more": True,
                    "next_offset": 20 if offset == "0" else 0}
        if "to_uid" in extra:
            return {"status_code": 0, "data": [], "has_more": False}
        return {"status_code": 0, "data": []}

    with mobile_requests(response) as calls:
        items, source = fetch()
    assert items is None
    assert "partial author results" in source
    assert "repeated" in source
    assert "author endpoints complete" not in source
    assert len([call for call in calls if call[0] == media.STORY_PROFILE_LIST_PATH]) == 2


def test_author_page_limit_preserves_first_page_and_warns():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            offset = int(extra["offset"])
            return {"status_code": 0, "data": [story(str(offset))],
                    "has_more": True, "next_offset": offset + 20}
        return {"status_code": 0, "data": [], "has_more": False}

    with mobile_requests(response) as calls:
        items, source = fetch()
    assert len(items) == 15
    assert "page limit" in source
    assert len([call for call in calls if call[0] == media.STORY_PROFILE_LIST_PATH]) == 15


def test_tray_filters_foreign_or_missing_authors_but_accepts_numeric_uid():
    numeric_only = story("numeric", author=None)
    numeric_only["author"] = {"uid": UID}

    def response(path, extra, _host):
        if "to_uid" in extra:
            return {"status_code": 1, "status_msg": "unavailable"}
        return {"status_code": 0, "data": [story("foreign", author="someone-else"),
                story("unverified", author=None), story("correct"), numeric_only]}

    with mobile_requests(response):
        items, _source = fetch()
    assert [item["aweme_id"] for item in items] == ["correct", "numeric"]


def test_tray_pack_author_can_verify_a_nested_story():
    def response(path, extra, _host):
        if "to_uid" in extra:
            return {"status_code": 1}
        return {"status_code": 0, "data": [{"user": {"sec_uid": AUTHOR},
                "story_list": [story("pack-story", author=None)]}]}

    with mobile_requests(response):
        items, _source = fetch()
    assert [item["aweme_id"] for item in items] == ["pack-story"]


def test_missing_status_does_not_masquerade_as_successful_empty_author_response():
    def response(path, extra, _host):
        return {"data": None} if "to_uid" in extra else {
            "status_code": 0, "data": [story("tray-story")]}

    with mobile_requests(response):
        items, source = fetch()
    assert [item["aweme_id"] for item in items] == ["tray-story"]
    assert "partial author results" in source


@pytest.mark.parametrize("error", [InterruptedError("cancelled"), media.LoginRequiredError("expired")])
def test_request_cancellation_and_login_errors_propagate(error):
    def response(_path, _extra, _host):
        raise error

    with mobile_requests(response), pytest.raises(type(error)):
        fetch()


def test_server_login_rejection_propagates_instead_of_falling_back():
    with mobile_requests(lambda *_args: {"status_code": 2483}), pytest.raises(media.LoginRequiredError):
        fetch()


def test_actual_story_source_is_called_before_post_fallback():
    with patch.object(media, "fetch_stories_via_mobile_post_api") as post, patch.object(
        media, "fetch_stories_via_mobile_story_feed", return_value=([story("active")], "story/feed")
    ):
        items, _source, _supported = media.fetch_stories(object(), {}, AUTHOR)
    assert [item["aweme_id"] for item in items] == ["active"]
    post.assert_not_called()


def test_post_feed_cursor_cycle_is_bounded_and_deduplicates_retained_stories():
    calls = []

    def response(_client, _path, extra, *_args):
        calls.append(extra["max_cursor"])
        return httpx.Response(200, json={"status_code": 0, "aweme_list": [story("archive")],
                    "has_more": True, "max_cursor": "20" if extra["max_cursor"] == "0" else "0"})

    with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
        media, "_persistent_mobile_device", return_value=("1", "2")
    ), patch.object(media, "_mobile_signed_get", side_effect=response), patch.object(media, "_cancel_wait"):
        items, source = media.fetch_stories_via_mobile_post_api(object(), AUTHOR, cookie_header="test")
    assert [item["aweme_id"] for item in items] == ["archive"]
    assert calls == ["0", "20"]
    assert "repeated" in source


def test_post_scan_resumes_beyond_fifteen_pages_and_refreshes_first_page():
    calls = []
    check_number = 1
    scan_state = {}

    def response(_client, _path, extra, *_args):
        cursor = int(extra["max_cursor"])
        calls.append(cursor)
        items = [story("new-today")] if cursor == 0 and check_number == 2 else []
        if cursor == 300:
            items.append(story("2025-archive"))
        return httpx.Response(200, json={"status_code": 0, "aweme_list": items,
                "has_more": cursor < 300, "max_cursor": cursor + 20})

    with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
        media, "_persistent_mobile_device", return_value=("1", "2")
    ), patch.object(media, "_mobile_signed_get", side_effect=response), patch.object(media, "_cancel_wait"):
        first_items, first_source = media.fetch_stories_via_mobile_post_api(
            object(), AUTHOR, cookie_header="sessionid=test", scan_state=scan_state)
        assert first_items is None
        assert "page limit" in first_source
        assert scan_state["story_scan"]["post_cursor"] == "300"
        assert len(calls) == 15
        calls.clear()
        check_number = 2
        items, source = media.fetch_stories_via_mobile_post_api(
            object(), AUTHOR, cookie_header="sessionid=test", scan_state=scan_state)
    assert calls == [0, 300]
    assert [item["aweme_id"] for item in items] == ["new-today", "2025-archive"]
    assert "partial" not in source
    assert "post_cursor" not in scan_state["story_scan"]


def test_author_scan_resumes_next_offset_and_refreshes_first_page():
    scan_state = {}
    check_number = 1

    def response(path, extra, _host):
        if path != media.STORY_PROFILE_LIST_PATH:
            return {"status_code": 0, "data": [], "has_more": False}
        offset = int(extra["offset"])
        items = [story("fresh")] if offset == 0 and check_number == 2 else []
        if offset == 300:
            items.append(story("2025-profile-archive"))
        return {"status_code": 0, "data": items, "has_more": offset < 300,
                "next_offset": offset + 20}

    with mobile_requests(response) as calls:
        first_items, first_source = fetch(scan_state=scan_state)
        assert first_items is None
        assert "page limit" in first_source
        assert scan_state["story_scan"]["author_offsets"][media.STORY_PROFILE_LIST_PATH] == "300"
        assert len([call for call in calls if call[0] == media.STORY_PROFILE_LIST_PATH]) == 15
        calls.clear()
        check_number = 2
        items, source = fetch(scan_state=scan_state)
    assert [extra["offset"] for path, extra, _ in calls
            if path == media.STORY_PROFILE_LIST_PATH] == ["0", "300"]
    assert [item["aweme_id"] for item in items] == ["fresh", "2025-profile-archive"]
    assert "author endpoints complete" in source
    assert scan_state["story_scan"]["author_offsets"] == {}


def test_scan_continuation_resets_when_login_or_target_changes():
    state = {}
    original = media._story_scan_state(state, AUTHOR, "sessionid=first")
    original["post_cursor"] = "300"
    original["author_offsets"][media.STORY_PROFILE_LIST_PATH] = "300"
    second_account = media._story_scan_state(state, AUTHOR, "sessionid=second")
    assert "post_cursor" not in second_account
    assert second_account["author_offsets"] == {}
    assert "sessionid" not in str(state)
    second_account["post_cursor"] = "300"
    other_target = media._story_scan_state(state, "different-target", "sessionid=second")
    assert "post_cursor" not in other_target


def test_main_story_lookup_passes_scan_state_to_author_requests():
    state = {}
    with patch.object(media, "fetch_stories_via_mobile_story_feed",
                      return_value=([story("active")], "story/feed")) as feed:
        media.fetch_stories(object(), {}, AUTHOR, scan_state=state)
    assert feed.call_args.kwargs["scan_state"] is state


def test_complete_empty_author_feeds_still_reach_retained_post_on_page_sixteen():
    state = {}
    post_calls = []
    now = 1791633600

    def post_response(_client, _path, extra, *_args):
        cursor = int(extra["max_cursor"])
        post_calls.append(cursor)
        # The ordinary 2022 video's only positive flag is TTL; it must never
        # become a story even when the author feeds are successfully empty.
        items = [{"aweme_id": "ordinary-2022", "story_ttl": 1,
                  "create_time": 1654777267, "author": {"sec_uid": AUTHOR}}]
        if cursor == 300:
            items.append({**story("2025-retained"), "create_time": 1735689600})
        return httpx.Response(200, json={"status_code": 0, "aweme_list": items,
                              "has_more": cursor < 300, "max_cursor": cursor + 20})

    with mobile_requests(lambda *_args: empty_profile()), patch.object(
        media, "_mobile_signed_get", side_effect=post_response
    ), patch.object(media.time, "time", return_value=now), patch.object(
        media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
    ), patch.object(media, "request_life_feed", return_value=(None, "no web life")), patch.object(
        media, "request_json", return_value={"status_code": 0, "data": []}
    ), patch.object(media, "profile_has_active_story", return_value=False):
        first = media.fetch_stories(object(), {"cookies": "sessionid=test"},
                                    AUTHOR, user_id=UID, scan_state=state)
        assert first[0] == []
        assert not first.complete
        assert "page limit" in first[1]
        assert post_calls == list(range(0, 300, 20))
        post_calls.clear()
        second = media.fetch_stories(object(), {"cookies": "sessionid=test"},
                                     AUTHOR, user_id=UID, scan_state=state)
    assert post_calls == [0, 300]
    assert [item["aweme_id"] for item in second[0]] == ["2025-retained"]
    assert second.complete
    assert "post-feed fallback" in second[1]


def test_available_life_items_keep_overall_scan_incomplete_after_author_failure():
    with patch.object(media, "fetch_stories_via_mobile_story_feed", return_value=
                      media.MobileStoryFeedResult(None, "history unavailable", complete=False)), patch.object(
        media, "fetch_stories_via_mobile_life_feed", return_value=([story("life")], "life/feed")
    ):
        result = media.fetch_stories(object(), {}, AUTHOR)
    assert [item["aweme_id"] for item in result[0]] == ["life"]
    assert not result.complete
    assert "history unavailable" in result[1]


def test_post_scan_cycle_clears_saved_cursor_to_allow_recovery():
    state = {}

    def response(_client, _path, _extra, *_args):
        return httpx.Response(200, json={"status_code": 0, "aweme_list": [],
                                        "has_more": True, "max_cursor": 0})

    with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
        media, "_persistent_mobile_device", return_value=("1", "2")
    ), patch.object(media, "_mobile_signed_get", side_effect=response):
        result = media.fetch_stories_via_mobile_post_api(object(), AUTHOR,
                                                       cookie_header="test", scan_state=state)
    assert not result.complete
    assert "repeated" in result[1]
    assert "post_cursor" not in state["story_scan"]


def test_author_scan_cycle_clears_saved_offset_to_allow_recovery():
    state = {}

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return {"status_code": 0, "data": [], "has_more": True, "next_offset": 0}
        return {"status_code": 0, "data": [], "has_more": False}

    with mobile_requests(response):
        result = fetch(scan_state=state)
    assert not result.complete
    assert "repeated" in result[1]
    assert state["story_scan"]["author_offsets"] == {}


@pytest.mark.parametrize("cursor", [False, "not-a-cursor", -1, "0" * 33])
def test_invalid_post_cursor_stops_without_a_second_request(cursor):
    payload = {"status_code": 0, "aweme_list": [], "has_more": True, "max_cursor": cursor}
    with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
        media, "_persistent_mobile_device", return_value=("1", "2")
    ), patch.object(media, "_mobile_signed_get", return_value=httpx.Response(200, json=payload)) as request:
        result = media.fetch_stories_via_mobile_post_api(object(), AUTHOR, cookie_header="test")
    assert not result.complete
    assert "invalid pagination cursor" in result[1]
    request.assert_called_once()


def test_active_bucket_more_flag_is_not_silently_declared_complete():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return {"status_code": 0, "active_data": {"data": [], "has_more": True},
                    "has_more": False, "next_offset": 0}
        return {"status_code": 0, "data": [], "has_more": False}

    with mobile_requests(response):
        result = fetch()
    assert result[0] is None
    assert not result.complete
    assert "active pagination cursor" in result[1]
