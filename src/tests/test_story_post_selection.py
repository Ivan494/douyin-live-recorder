"""Post-feed TTL metadata must not hide a currently visible story pack."""
from unittest.mock import patch

import httpx

import douyin_media_downloader as media


NOW = 1791633600
AUTHOR = "bailu-test-author"


def old_post():
    # Sanitized fields from the real June 9, 2022 post 7107214234903186695.
    return {
        "aweme_id": "7107214234903186695",
        "aweme_type": 0,
        "create_time": 1654777267,
        "is_story": 0,
        "is_25_story": 0,
        "is_24_story": 0,
        "is_moment_story": 0,
        "story_ttl": 1,
        "author": {"sec_uid": AUTHOR},
    }


def test_real_old_ttl_post_does_not_mask_current_story():
    active = {"aweme_id": "active-ring", "is_25_story": 1,
              "author": {"sec_uid": AUTHOR}}
    request = httpx.Request("GET", "https://aweme.snssdk.com/aweme/v1/aweme/post/")
    response = httpx.Response(200, request=request, json={
        "status_code": 0, "aweme_list": [old_post()], "has_more": 0,
    })
    with patch.object(media.time, "time", return_value=NOW), \
         patch.object(media, "_check_mobile_signer", return_value=True), \
         patch.object(media, "_persistent_mobile_device", return_value=("1", "2")), \
         patch.object(media, "_mobile_signed_get", return_value=response), \
         patch.object(media, "fetch_stories_via_mobile_story_feed",
                      return_value=([active], "actual active pack")) as feed:
        items, source, supported = media.fetch_stories(
            object(), {"cookies": "sessionid=test"}, AUTHOR
        )
    assert [item["aweme_id"] for item in items] == ["active-ring"]
    assert supported
    assert source == "actual active pack"
    feed.assert_called_once()


def test_ttl_only_post_requires_recent_creation_time():
    with patch.object(media.time, "time", return_value=NOW):
        assert not media._is_mobile_post_story(old_post())
        recent = {**old_post(), "create_time": NOW - 3600}
        assert media._is_mobile_post_story(recent)
        unknown = {key: value for key, value in recent.items() if key != "create_time"}
        assert not media._is_mobile_post_story(unknown)


def test_explicit_story_marker_keeps_retained_story_compatibility():
    with patch.object(media.time, "time", return_value=NOW):
        assert media._is_mobile_post_story({**old_post(), "is_25_story": 1})
