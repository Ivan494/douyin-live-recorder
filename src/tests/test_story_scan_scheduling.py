"""Active catalogs and profile archives must share bounded, resumable scans."""
import json

import pytest

import douyin_media_downloader as media
from .test_story_history_retrieval import (
    AUTHOR, catalog_head, catalog_page, fetch, mobile_requests, story,
)


def seeded_state(offset):
    state = {}
    media._story_scan_state(state, AUTHOR, "sessionid=test")["author_offsets"][
        media.STORY_PROFILE_LIST_PATH] = str(offset)
    return state


def profile_calls(calls):
    return [extra for path, extra, _host in calls if path == media.STORY_PROFILE_LIST_PATH]


def assert_profile_budget(calls):
    assert sum(path == media.STORY_PROFILE_LIST_PATH or (
        path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra
    ) for path, extra, _host in calls) <= 15


def successful_empty():
    return {"status_code": 0, "data": [], "has_more": False}


def test_budget_filling_catalog_finishes_with_archive_within_two_polls():
    state = {}

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            if extra["offset"] == "0":
                return catalog_head(104, archive=True)
            assert extra["offset"] == "20"
            return {**successful_empty(), "data": [story("archive-only")]}
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            return catalog_page(extra, 104)
        return successful_empty()

    seen = set()
    with mobile_requests(response) as calls:
        for poll in range(2):
            start = len(calls)
            result = fetch(scan_state=state)
            assert_profile_budget(calls[start:])
            seen.update(item["aweme_id"] for item in result[0])
            assert result.complete == (poll == 1)
            if poll == 0:
                assert "archive-only" in seen
                assert state["story_scan"]["profile_archive_complete"] is True
    assert seen == {str(1000 + n) for n in range(104)} | {"archive-only"}


@pytest.mark.parametrize("pagination", [{}, {"has_more": False, "next_offset": 0}])
def test_enlarged_catalog_reserves_saved_archive_head_and_finishes(pagination):
    state = seeded_state(300)

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            if extra["offset"] != "0":
                assert extra["offset"] == "300"
                assert extra["active_data_size"] == "20"
                return {**successful_empty(), "data": [story("archive-only")]}
            head = catalog_head(118, archive=True)
            if extra["active_data_size"] == "20":
                head["active_data"].pop("story_group_v2_items")
                return head
            assert extra["active_data_size"] == "40"
            head["active_data"]["data"] = [story(str(1000 + n)) for n in range(40)]
            head["active_data"].pop("has_more")
            return {"status_code": 0, "active_data": head["active_data"], **pagination}
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            return catalog_page(extra, 118)
        return successful_empty()

    seen = set()
    with mobile_requests(response) as calls:
        for poll in range(2):
            start = len(calls)
            result = fetch(scan_state=state)
            assert_profile_budget(calls[start:])
            seen.update(item["aweme_id"] for item in result[0])
            assert result.complete == (poll == 1)
            if poll == 0:
                assert "300" in [extra["offset"] for extra in profile_calls(calls)]
                assert "archive-only" in seen
    assert seen == {str(1000 + n) for n in range(118)} | {"archive-only"}


def replay_response(path, extra, _host):
    if path == media.STORY_PROFILE_LIST_PATH:
        offset = int(extra["offset"])
        if offset == 0:
            return catalog_head(110, archive=True)
        assert offset in range(20, 261, 20)
        return {"status_code": 0, "data": [story(f"archive-{offset}")],
                "has_more": offset < 260, "next_offset": offset + 20}
    if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
        return catalog_page(extra, 110)
    return successful_empty()


@pytest.mark.parametrize("reload_state", [False, True])
def test_resumed_scan_completes_every_two_polls_within_parent_request_budget(reload_state):
    state = seeded_state(260)
    complete_polls = 0
    with mobile_requests(replay_response) as calls:
        for poll in range(100):
            start = len(calls)
            result = fetch(scan_state=state)
            assert_profile_budget(calls[start:])
            # Bounded completion: the seeded scan and each fresh scan finish
            # in two checks, including when the process reloads saved JSON.
            assert result.complete == (poll % 2 == 1)
            complete_polls += result.complete
            if poll == 0:
                assert state["story_scan"]["profile_archive_complete"] is True
                assert [extra["offset"] for extra in profile_calls(calls)] == ["0", "260"]
            if poll == 1:
                assert [extra["offset"] for extra in profile_calls(calls[start:])] == ["0"]
                assert "active_story_ids" not in state["story_scan"]
            if reload_state:
                state = json.loads(json.dumps(state))
    # Count EVERY mobile request, including other author sources and trays.
    assert len(calls) <= 1738
    assert complete_polls >= 50
    print(f"100-poll replay (JSON reload={reload_state}): "
          f"{len(calls)} total requests, {complete_polls} complete polls")


def test_active_completion_survives_until_long_archive_finishes():
    state = {}

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            offset = int(extra["offset"])
            if offset == 0:
                return catalog_head(40, archive=True)
            return {"status_code": 0, "data": [story(f"archive-{offset}")],
                    "has_more": offset < 700, "next_offset": offset + 20}
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            return catalog_page(extra, 40)
        return successful_empty()

    with mobile_requests(response) as calls:
        for poll in range(3):
            start = len(calls)
            result = fetch(scan_state=state)
            assert_profile_budget(calls[start:])
            assert result.complete == (poll == 2)
            if poll < 2:
                assert len(state["story_scan"]["active_story_ids"]) == 40
            if poll:
                assert not any("direct" in extra for _path, extra, _host in calls[start:])
            state = json.loads(json.dumps(state))
    assert "archive-700" in {item["aweme_id"] for item in result[0]}


@pytest.mark.parametrize("catalog_size", [40, 80, 160, 320])
def test_late_catalog_preserves_completion_across_repeated_enlargements(catalog_size):
    state = seeded_state(260)
    count = catalog_size + 90

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            if extra["offset"] != "0":
                assert extra["offset"] == "260"
                return {**successful_empty(), "data": [story("archive-only")]}
            size = int(extra["active_data_size"])
            head = catalog_head(count, archive=True)
            head["active_data"]["data"] = [story(str(1000 + n)) for n in range(size)]
            if size < catalog_size:
                head["active_data"].pop("story_group_v2_items")
            return head
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            return catalog_page(extra, count)
        return successful_empty()

    seen = set()
    with mobile_requests(response) as calls:
        for poll in range(2):
            start = len(calls)
            result = fetch(scan_state=state)
            assert_profile_budget(calls[start:])
            assert result.complete == (poll == 1)
            seen.update(item["aweme_id"] for item in result[0])
            state = json.loads(json.dumps(state))
    assert seen == {str(1000 + n) for n in range(count)} | {"archive-only"}


def test_completed_active_catalog_is_invalidated_while_archive_is_pending():
    state = {}
    changed = False

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            offset = int(extra["offset"])
            if offset == 0:
                head = catalog_head(40, archive=True)
                if changed:
                    head["active_data"]["story_group_v2_items"][0]["story_group_v2"]["max_cursor"] += 1
                return head
            return {"status_code": 0, "data": [story(f"archive-{offset}")],
                    "has_more": offset < 700, "next_offset": offset + 20}
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            page = catalog_page({**extra, "max_cursor": "100039"})
            if changed:
                page["story_group"]["max_cursor"] += 1
            return page
        return successful_empty()

    with mobile_requests(response) as calls:
        assert not fetch(scan_state=state).complete
        assert len(state["story_scan"]["active_story_ids"]) == 40
        assert state["story_scan"]["author_offsets"][media.STORY_PROFILE_LIST_PATH] == "220"
        changed = True
        calls.clear()
        assert not fetch(scan_state=state).complete
        assert_profile_budget(calls)
    assert [extra["offset"] for extra in profile_calls(calls)][1] == "20"
    assert [extra["cursor"] for _path, extra, _host in calls if "direct" in extra][0] == "100021"


def test_unfinished_active_bucket_on_archive_page_is_not_cached_as_complete():
    state = {}

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            if extra["offset"] == "0":
                return catalog_head(40, archive=True)
            assert extra["offset"] == "20"
            return {**successful_empty(), "active_data": {
                "data": [story("uncatalogued")], "has_more": True}}
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            return catalog_page(extra)
        return successful_empty()

    with mobile_requests(response) as calls:
        for _poll in range(2):
            start = len(calls)
            result = fetch(scan_state=state)
            assert not result.complete
            assert result.scan_reason == "pagination"
            assert [extra["offset"] for extra in profile_calls(calls[start:])] == ["0", "20"]
            assert not state["story_scan"].get("profile_archive_complete")
            assert_profile_budget(calls[start:])


@pytest.mark.parametrize("change", ["entries", "min_cursor", "max_cursor", "head", "next_offset", "no_catalog"])
@pytest.mark.parametrize("archive_finished", [False, True])
def test_catalog_or_head_change_invalidates_saved_progress(change, archive_finished):
    state = seeded_state(260)
    changed = False

    def update_catalog(group):
        if change == "entries":
            # Same IDs, different explicit cursor: intersection of IDs alone
            # must not allow old progress to skip the changed catalog.
            group["story_id_list"][20]["cursor"] += 1
        elif change == "min_cursor":
            group["min_cursor"] -= 1
        elif change == "max_cursor":
            group["max_cursor"] += 1

    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            if extra["offset"] != "0":
                offset = int(extra["offset"])
                return {"status_code": 0, "data": [story(f"archive-{offset}")],
                        "has_more": not archive_finished, "next_offset": offset + 20}
            head = catalog_head(110, archive=True)
            if changed:
                update_catalog(head["active_data"]["story_group_v2_items"][0]["story_group_v2"])
                if change == "head":
                    head["data"] = [story("new-archive-head")]
                elif change == "next_offset":
                    head["next_offset"] = 40
                elif change == "no_catalog":
                    head["active_data"].pop("story_group_v2_items")
                    head["active_data"]["has_more"] = False
            return head
        if path == media.NEW_STORY_FEED_V2_PATH and "direct" in extra:
            # Reuse the existing pager helper while adjusting only the changed
            # metadata to keep all returned cursors/catalogs server-supplied.
            page = catalog_page({**extra, "min_cursor": "100000", "max_cursor": "100109"}, 110)
            if changed:
                update_catalog(page["story_group"])
                if change == "entries" and extra["cursor"] == "100022":
                    page["story_group"]["story_list"].insert(0, {"story": story("1020")})
            return page
        return successful_empty()

    with mobile_requests(response) as calls:
        assert not fetch(scan_state=state).complete
        state = json.loads(json.dumps(state))
        changed = True
        calls.clear()
        result = fetch(scan_state=state)
        assert_profile_budget(calls)
    offsets = [extra["offset"] for extra in profile_calls(calls)]
    assert offsets[1] == ("40" if change == "next_offset" else "20")
    if change != "no_catalog":
        cursors = [extra["cursor"] for _path, extra, _host in calls if "direct" in extra]
        assert cursors[0] == ("100022" if change == "entries" else "100021")
        assert "1020" in {item["aweme_id"] for item in result[0]}


@pytest.mark.parametrize("change", ["login", "target"])
def test_new_progress_fields_follow_existing_scope_reset(change):
    state = seeded_state(260)
    with mobile_requests(replay_response):
        assert not fetch(scan_state=state).complete
    assert state["story_scan"]["profile_archive_complete"] is True
    continuation = media._story_scan_state(
        state, "other-target" if change == "target" else AUTHOR,
        "sessionid=other" if change == "login" else "sessionid=test")
    assert continuation["author_offsets"] == {}
    assert "active_story_ids" not in continuation
    assert "profile_scan_key" not in continuation
    assert "profile_archive_complete" not in continuation
