"""Completed item IDs must not hide missing or damaged album assets."""

from pathlib import Path

import httpx
import pytest

import douyin_media_downloader as media


IMAGE_BODY = b"\xff\xd8\xff\xe0" + b"i" * 4096


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: "")
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "")


def album():
    return {
        "aweme_id": "7692046175923311247",
        "desc": "album repair",
        "create_time": 1790944062,
        "images": [{"url_list": [f"https://video-web-cn.douyin.com/{index}.jpg"]} for index in (1, 2)],
    }


def album_paths(tmp_path, aweme):
    stem = media.media_item_path(tmp_path / "images", aweme, suffix="")
    return [Path(f"{stem}_{index:02d}.jpg") for index in (1, 2)]


@pytest.mark.parametrize("completed_id", [False, True])
@pytest.mark.parametrize("damaged", [False, True])
def test_missing_or_damaged_second_still_is_repaired_and_first_is_reused(tmp_path, completed_id, damaged):
    aweme = album()
    first, second = album_paths(tmp_path, aweme)
    first.parent.mkdir(parents=True)
    first.write_bytes(IMAGE_BODY)
    if damaged:
        second.write_bytes(b"partial image")
    state = {"downloaded_video_ids": [aweme["aweme_id"]] if completed_id else []}
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request.url.path) or httpx.Response(200, content=IMAGE_BODY))) as client:
        result = media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video")
    assert result.downloaded == 1 and result.failed == 0
    assert calls == ["/2.jpg"]
    assert first.read_bytes() == second.read_bytes() == IMAGE_BODY
    assert state["downloaded_video_ids"] == [aweme["aweme_id"]]


def test_failed_static_repair_clears_stale_completed_id(tmp_path):
    aweme = album()
    first, _second = album_paths(tmp_path, aweme)
    first.parent.mkdir(parents=True)
    first.write_bytes(IMAGE_BODY)
    state = {"downloaded_video_ids": [aweme["aweme_id"]]}
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))) as client:
        result = media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video")
    assert result.failed == 1 and result.skipped == 0
    assert state["downloaded_video_ids"] == []


def test_complete_static_album_is_reused_without_completed_id(tmp_path):
    aweme = album()
    paths = album_paths(tmp_path, aweme)
    paths[0].parent.mkdir(parents=True)
    for path in paths:
        path.write_bytes(IMAGE_BODY)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(503))) as client:
        result = media.download_aweme_items(client, {}, [aweme], tmp_path, {}, "video")
    assert result.skipped == 1 and result.failed == 0
    assert calls == []


def test_image_entries_preserve_duplicates_and_original_positions():
    aweme = album()
    aweme["images"].insert(0, {})
    aweme["images"][2]["url_list"] = aweme["images"][1]["url_list"][:]
    entries = media.collect_image_entries(aweme)
    assert [entry["index"] for entry in entries] == [2, 3]
    assert entries[0]["url"] == entries[1]["url"]


def test_image_entries_keep_declared_motion_without_downloadable_urls():
    aweme = {"images": [{"video": {"duration": 2917}}]}
    entries = media.collect_image_entries(aweme)
    assert entries == [{"index": 1, "url": "", "video_urls": [], "live_photo": True}]


def test_failed_repair_is_removed_from_persisted_completed_state(tmp_path):
    aweme = album()
    state_path, state = media.load_state(tmp_path)
    state["downloaded_video_ids"] = [aweme["aweme_id"]]
    media.save_state(state_path, state)
    first, _second = album_paths(tmp_path, aweme)
    first.parent.mkdir(parents=True)
    first.write_bytes(IMAGE_BODY)
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))) as client:
        result = media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video")
    assert result.failed == 1
    media.save_state(state_path, state)
    _path, reloaded = media.load_state(tmp_path)
    assert reloaded["downloaded_video_ids"] == []
    assert not any(key.startswith("_invalidated") for key in reloaded)


def test_repair_state_merge_preserves_another_completed_item(tmp_path):
    aweme = album()
    state_path, state = media.load_state(tmp_path)
    state["downloaded_video_ids"] = [aweme["aweme_id"]]
    media.save_state(state_path, state)
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))) as client:
        media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video")
    _path, parallel_state = media.load_state(tmp_path)
    parallel_state["downloaded_video_ids"].append("another-completed-item")
    media.save_state(state_path, parallel_state)
    media.save_state(state_path, state)
    _path, reloaded = media.load_state(tmp_path)
    assert reloaded["downloaded_video_ids"] == ["another-completed-item"]


def test_successful_retry_removes_tombstone_before_state_merge(tmp_path):
    aweme = album()
    state_path, state = media.load_state(tmp_path)
    state["downloaded_video_ids"] = [aweme["aweme_id"]]
    media.save_state(state_path, state)
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))) as client:
        media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video")
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=IMAGE_BODY))) as client:
        result = media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video")
    assert result.downloaded == 1
    media.save_state(state_path, state)
    _path, reloaded = media.load_state(tmp_path)
    assert reloaded["downloaded_video_ids"] == [aweme["aweme_id"]]


def test_failed_repair_after_periodic_checkpoint_keeps_its_tombstone(tmp_path):
    aweme = album()
    state_path, state = media.load_state(tmp_path)
    state["downloaded_video_ids"] = [aweme["aweme_id"]]
    media.save_state(state_path, state)
    videos = [{"aweme_id": f"video-{index}", "create_time": 1790944062,
               "video": {"play_addr": {"url_list": ["https://video-web-cn.douyin.com/video.mp4"]}}}
              for index in range(10)]
    video_body = b"\x00\x00\x00\x18ftypisom" + b"v" * 4096

    def respond(request):
        return httpx.Response(200, content=video_body) if request.url.path.endswith(".mp4") else httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {}, videos + [aweme], tmp_path, state, "video")
    assert result.downloaded == 10 and result.failed == 1
    media.save_state(state_path, state)
    _path, reloaded = media.load_state(tmp_path)
    assert aweme["aweme_id"] not in reloaded["downloaded_video_ids"]
    assert len(reloaded["downloaded_video_ids"]) == 10
