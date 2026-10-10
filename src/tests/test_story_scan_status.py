import json
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

import douyin_media_downloader as media
import douyin_recorder_app as app


def profile(directory):
    return {"id": "story-scan-test", "name": "Test", "output_dir": str(directory),
            "original_profile_url": "https://www.douyin.com/user/synthetic-target"}


def test_incomplete_story_scan_is_not_reported_as_empty(tmp_path):
    incomplete = media.StoryFetchResult([], "partial author response", True, complete=False)
    with patch.object(media, "fetch_stories", return_value=incomplete), \
         patch.object(media, "_profile_cookie_header", return_value=""):
        summary = media.download_profile(profile(tmp_path), videos=False, stories=True)
    assert summary["stories"]["status"] == "partial"
    assert json.loads((tmp_path / "douyin_media_state.json").read_text())["last_summary"]["stories"]["status"] == "partial"
    assert "partial" not in app.MediaDownloadEngine.summarize_kind(summary, "stories").lower()


def test_story_scan_context_survives_between_real_profile_checks(tmp_path):
    checkpoints = []
    def fetch(_client, _profile, _sec, user_id="", scan_state=None):
        assert isinstance(scan_state, dict), "Profile checks must pass persistent scan state"
        checkpoints.append(dict(scan_state.get("story_scan", {})))
        if len(checkpoints) == 1:
            scan_state["story_scan"] = {"scope": "synthetic", "post_cursor": "16"}
            return media.StoryFetchResult([], "continue later", True, complete=False)
        scan_state["story_scan"] = {"scope": "synthetic"}
        return media.StoryFetchResult([], "complete empty", True, complete=True)
    with patch.object(media, "fetch_stories", side_effect=fetch), \
         patch.object(media, "_profile_cookie_header", return_value=""):
        first = media.download_profile(profile(tmp_path), videos=False, stories=True)
        second = media.download_profile(profile(tmp_path), videos=False, stories=True)
    assert first["stories"]["status"] == "partial"
    assert checkpoints == [{}, {"scope": "synthetic", "post_cursor": "16"}]
    assert second["stories"]["status"] == "no_active_stories"


@contextmanager
def unavailable_mobile(*, signer=True, cookie="", public_items=None):
    with ExitStack() as stack:
        stack.enter_context(patch.object(media, "_check_mobile_signer", return_value=signer))
        stack.enter_context(patch.object(media, "_mobile_cookie_header", return_value=cookie))
        stack.enter_context(patch.object(media, "_profile_cookie_header", return_value=cookie))
        stack.enter_context(patch.object(media, "fetch_stories_via_mobile_life_feed",
                                         return_value=(None, "mobile life unavailable")))
        stack.enter_context(patch.object(media, "request_life_feed", return_value=(
            {"status_code": 0, "user_story_list": [{"story_list": public_items or []}]},
            "https://example.test/public-life-feed")))
        stack.enter_context(patch.object(media, "request_json", return_value={"status_code": 0, "data": []}))
        stack.enter_context(patch.object(media, "profile_has_active_story", return_value=False))
        yield


@pytest.mark.parametrize("signer,cookie,expected", [
    (True, "", "login_required"),
    (False, "sessionid=test", "unavailable"),
])
def test_missing_mobile_precondition_is_not_an_unfinished_scan(tmp_path, signer, cookie, expected):
    with unavailable_mobile(signer=signer, cookie=cookie), patch.object(
        media, "_mobile_signed_request"
    ) as request, patch.object(media, "_mobile_signed_get") as post:
        summary = media.download_profile(profile(tmp_path), videos=False, stories=True)
    assert summary["stories"]["status"] == expected
    request.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("signer,cookie", [(True, ""), (False, "sessionid=test")])
def test_public_story_success_is_not_partial_when_mobile_scan_was_unavailable(tmp_path, signer, cookie):
    public = {"aweme_id": "public-story", "author": {"sec_uid": "synthetic-target"},
              "is_25_story": 1}
    with unavailable_mobile(signer=signer, cookie=cookie, public_items=[public]), patch.object(
        media, "download_aweme_items", return_value=media.MediaResult(status="ok", checked=1, skipped=1)
    ):
        summary = media.download_profile(profile(tmp_path), videos=False, stories=True)
    assert summary["stories"]["status"] == "ok"


def test_actual_started_scan_failure_stays_partial_with_empty_web_fallback(tmp_path):
    failed = media.MobileStoryFeedResult(None, "author request timed out", complete=False)
    with unavailable_mobile(signer=True, cookie="sessionid=test"), patch.object(
        media, "fetch_stories_via_mobile_story_feed", return_value=failed
    ), patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no stories")):
        summary = media.download_profile(profile(tmp_path), videos=False, stories=True)
    assert summary["stories"]["status"] == "partial"


@pytest.mark.parametrize("signer,cookie,reason", [
    (True, "", "login_required"),
    (False, "sessionid=test", "signer_unavailable"),
])
def test_skipped_mobile_result_has_explicit_capability_metadata(signer, cookie, reason):
    with unavailable_mobile(signer=signer, cookie=cookie):
        author = media.fetch_stories_via_mobile_story_feed(object(), "target", cookie_header=cookie)
        posts = media.fetch_stories_via_mobile_post_api(object(), "target", cookie_header=cookie)
        overall = media.fetch_stories(object(), {}, "target")
    assert not author.attempted and not posts.attempted
    assert not author.complete and not posts.complete
    assert author.unavailable_reason == posts.unavailable_reason == reason
    assert overall.complete
    assert overall.unavailable_reason == reason


def test_successful_public_result_clears_mobile_capability_warning():
    public = {"aweme_id": "public-story", "author": {"sec_uid": "target"}}
    with unavailable_mobile(signer=True, cookie="", public_items=[public]):
        result = media.fetch_stories(object(), {}, "target")
    assert result.complete
    assert result.unavailable_reason == ""
    assert [item["aweme_id"] for item in result[0]] == ["public-story"]
