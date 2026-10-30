"""Full-fetch joins must retain validated author scans until posts catch up."""
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import httpx
import pytest

import douyin_media_downloader as media
from .test_story_history_retrieval import AUTHOR, UID, mobile_requests, story
from .test_story_scan_scheduling import assert_profile_budget, successful_empty


def fetch_all(state, *, author=AUTHOR, cookie="sessionid=test"):
    return media.fetch_stories(object(), {"cookies": cookie}, author,
                               user_id=UID, scan_state=state)


def legacy_checkpoint():
    state = {}
    scan = media._story_scan_state(state, AUTHOR, "sessionid=test")
    scan["author_offsets"][media.STORY_FEED_PATH] = "580"
    scan["post_cursor"] = "580"
    return state


@pytest.fixture(scope="module")
def legacy_functions():
    # Execute the actual pre-upgrade implementation without checking it out.
    # Include its caller as well: its author helper predates the defer option.
    source = subprocess.check_output(
        ["git", "show", "e7fe8e6:src/douyin_media_downloader.py"],
        cwd=Path(__file__).resolve().parents[2], text=True)
    names = {"fetch_stories", "fetch_stories_via_mobile_story_feed"}
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<e7fe8e6>", "exec"),
         media.__dict__, namespace)
    return namespace


@contextmanager
def retained_post_requests(author_response=None, *, post_items=True):
    post_calls = []

    def response(path, extra, host):
        if author_response is not None:
            overridden = author_response(path, extra, host)
            if overridden is not None:
                return overridden
        if path == media.STORY_PROFILE_LIST_PATH or (path == media.STORY_FEED_PATH and "to_uid" in extra):
            offset = int(extra["offset"])
            last = 300 if path == media.STORY_PROFILE_LIST_PATH else 580
            assert offset in range(0, last + 1, 20)
            return {**successful_empty(), "has_more": offset < last, "next_offset": offset + 20}
        return successful_empty()

    def post_response(_client, _path, extra, *_args):
        cursor = int(extra["max_cursor"])
        assert cursor in range(0, 581, 20)
        post_calls.append(cursor)
        return httpx.Response(200, json={
            "status_code": 0,
            "aweme_list": [story("post-only")] if post_items and cursor == 580 else [],
            "has_more": cursor < 580, "max_cursor": cursor + 20,
        }, request=httpx.Request("GET", "https://example.test"))

    with mobile_requests(response) as calls, patch.object(
        media, "_mobile_signed_get", side_effect=post_response
    ), patch.object(media, "fetch_stories_via_mobile_life_feed", return_value=(None, "empty life")), patch.object(
        media, "request_life_feed", return_value=(None, "empty web life")
    ), patch.object(media, "request_json", return_value=successful_empty()), patch.object(
        media, "profile_has_active_story", return_value=False
    ):
        yield calls, post_calls


@pytest.mark.parametrize("seed_from_parent", [False, True])
@pytest.mark.parametrize("reload_state", [False, True])
@pytest.mark.parametrize("revision", ["e7fe8e6", "working"])
def test_empty_author_and_retained_post_scans_complete_within_four_polls(
        legacy_functions, seed_from_parent, reload_state, revision):
    state = legacy_checkpoint()
    with retained_post_requests() as (calls, post_calls):
        if seed_from_parent:
            state = {}
            with patch.object(media, "fetch_stories_via_mobile_story_feed",
                              legacy_functions["fetch_stories_via_mobile_story_feed"]):
                for _ in range(2):
                    legacy_functions["fetch_stories"](
                        object(), {"cookies": "sessionid=test"}, AUTHOR, user_id=UID, scan_state=state)
                    if reload_state:
                        state = json.loads(json.dumps(state))
            assert state == legacy_checkpoint()
        calls.clear()
        post_calls.clear()
        complete_polls = []
        seen = set()
        functions = legacy_functions if revision == "e7fe8e6" else {
            name: getattr(media, name) for name in legacy_functions}
        with patch.object(media, "fetch_stories_via_mobile_story_feed",
                          functions["fetch_stories_via_mobile_story_feed"]):
            for poll in range(1, 101):
                start, post_start = len(calls), len(post_calls)
                result = functions["fetch_stories"](
                    object(), {"cookies": "sessionid=test"}, AUTHOR, user_id=UID, scan_state=state)
                current_calls = calls[start:]
                assert_profile_budget(current_calls)
                assert sum(path == media.STORY_FEED_PATH and "to_uid" in extra
                           for path, extra, _ in current_calls) <= 15
                assert len(post_calls) - post_start <= 15
                # Retention never replaces the fresh heads of either author
                # source or the post source with a cached response.
                for path in (media.STORY_PROFILE_LIST_PATH, media.STORY_FEED_PATH):
                    assert next(extra["offset"] for p, extra, _ in current_calls
                                if p == path and "to_uid" in extra) == "0"
                assert post_calls[post_start] == 0
                seen.update(item["aweme_id"] for item in result[0])
                if result.complete:
                    complete_polls.append(poll)
                    assert "author_scans" not in state["story_scan"]
                    assert "post_cursor" not in state["story_scan"]
                if reload_state:
                    state = json.loads(json.dumps(state))
        assert "post-only" in seen
        assert complete_polls and complete_polls[0] <= 4
        assert max(b - a for a, b in zip(complete_polls, complete_polls[1:])) <= 6
        assert len(complete_polls) >= 17  # Measured e7fe8e6 baseline: 17/100.
        if revision == "e7fe8e6":
            assert complete_polls == list(range(4, 101, 6))
        print(f"full fetch {revision}, parent seed={seed_from_parent}, JSON reload={reload_state}: "
              f"{len(complete_polls)} completes, {len(calls) + len(post_calls)} mobile requests")


def pending_post_state():
    state = legacy_checkpoint()
    with retained_post_requests():
        for _ in range(2):
            assert not fetch_all(state).complete
    assert all(scan["complete"] for scan in state["story_scan"]["author_scans"].values())
    assert state["story_scan"]["post_cursor"] == "300"
    return json.loads(json.dumps(state))


@pytest.mark.parametrize("variant", [media.STORY_PROFILE_LIST_PATH, media.STORY_FEED_PATH])
@pytest.mark.parametrize("change", ["head", "next_offset", "timeout", "login", "cancel"])
def test_retained_author_completion_still_validates_each_head(variant, change):
    state = pending_post_state()

    def response(path, extra, _host):
        if path != variant or "to_uid" not in extra:
            return None
        if extra["offset"] == "0":
            if change == "timeout":
                raise httpx.ReadTimeout("synthetic head timeout")
            if change == "login":
                raise media.LoginRequiredError("synthetic login challenge")
            if change == "cancel":
                raise InterruptedError("synthetic cancellation")
            return {**successful_empty(), "data": [story("new-head")] if change == "head" else [],
                    "has_more": True, "next_offset": 40 if change == "next_offset" else 20}
        offset = int(extra["offset"])
        return {**successful_empty(), "has_more": offset < 700, "next_offset": offset + 20}

    with retained_post_requests(response) as (calls, _post_calls):
        if change in ("login", "cancel"):
            with pytest.raises(media.LoginRequiredError if change == "login" else InterruptedError):
                fetch_all(state)
        else:
            assert not fetch_all(state).complete
            assert_profile_budget(calls)
            offsets = [extra["offset"] for path, extra, _ in calls if path == variant and "to_uid" in extra]
            if change == "timeout":
                assert set(offsets) == {"0"}
            else:
                assert offsets[1] == ("40" if change == "next_offset" else "20")
                assert not state["story_scan"]["author_scans"][variant]["complete"]


@pytest.mark.parametrize("change", ["login", "target"])
def test_retained_author_completion_and_post_cursor_follow_scope_reset(change):
    state = pending_post_state()
    with retained_post_requests() as (calls, post_calls):
        result = fetch_all(state, author="other-target" if change == "target" else AUTHOR,
                           cookie="sessionid=other" if change == "login" else "sessionid=test")
        assert not result.complete
        assert_profile_budget(calls)
        for path in (media.STORY_PROFILE_LIST_PATH, media.STORY_FEED_PATH):
            offsets = [extra["offset"] for p, extra, _ in calls if p == path and "to_uid" in extra]
            assert offsets[:2] == ["0", "20"]
        assert post_calls[:2] == [0, 20]


@pytest.mark.parametrize("error", [httpx.ReadTimeout, media.LoginRequiredError, InterruptedError])
def test_post_failure_keeps_author_completion_for_a_successful_retry(error):
    state = pending_post_state()
    with retained_post_requests(), patch.object(media, "_mobile_signed_get", side_effect=error("synthetic")):
        if error is httpx.ReadTimeout:
            assert not fetch_all(state).complete
        else:
            with pytest.raises(error):
                fetch_all(state)
    state = json.loads(json.dumps(state))
    assert all(scan["complete"] for scan in state["story_scan"]["author_scans"].values())
    assert state["story_scan"]["post_cursor"] == "300"
    with retained_post_requests():
        assert not fetch_all(state).complete
        assert fetch_all(state).complete


@pytest.mark.parametrize("post_items", [False, True])
def test_completed_full_fetch_restarts_author_traversals(post_items):
    state = pending_post_state()
    with retained_post_requests(post_items=post_items) as (calls, _post_calls):
        assert not fetch_all(state).complete
        assert fetch_all(state).complete
        assert "author_scans" not in state["story_scan"]
        calls.clear()
        assert not fetch_all(state).complete
        for path in (media.STORY_PROFILE_LIST_PATH, media.STORY_FEED_PATH):
            offsets = [extra["offset"] for p, extra, _ in calls if p == path and "to_uid" in extra]
            assert offsets[:2] == ["0", "20"]
