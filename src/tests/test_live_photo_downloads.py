"""Live Photo assets at the item-download boundary; HTTP is synthetic.

The nested schema follows real posts 7692046175923311247 (one Live Photo)
and 7681610206682200296 (ten Live Photos), verified through the mobile API.
"""

from pathlib import Path

import httpx
import pytest

import douyin_media_downloader as media


COOKIE = "sessionid=synthetic-live-photo-session"
IMAGE_BODY = b"\xff\xd8\xff\xe0" + b"x" * 4096
VIDEO_BODY = b"\x00\x00\x00\x18ftypisom" + b"x" * 4096


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: "")
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "")


def live_photo_item(count=1):
    return {
        "aweme_id": "7692046175923311247",
        "aweme_type": 68,
        "desc": "Live Photo regression",
        "create_time": 1790944062,
        "video": {"play_addr": {"url_list": ["https://video-web-cn.douyin.com/bgm.mp3"]}},
        "images": [
            {"url_list": [f"https://video-web-cn.douyin.com/still-{index}.jpg"],
             "clip_type": 5 if count == 1 else 4, "live_photo_type": 1 if count == 1 else None,
             "video": {"duration": 2917,
                       "play_addr": {"url_list": [f"https://video-web-cn.douyin.com/motion-{index}.mp4"]}}}
            for index in range(count)
        ],
    }


def asset_paths(tmp_path, item, media_kind="video", index=1):
    folder = tmp_path / ("stories" if media_kind == "story" else "images")
    stem = folder / media.aweme_filename(item, suffix="")
    return Path(f"{stem}_{index:02d}.jpg"), Path(f"{stem}_{index:02d}_live.mp4")


@pytest.mark.parametrize("media_kind", ["video", "story"])
@pytest.mark.parametrize("count", [1, 10])
def test_live_photo_downloads_stills_and_motion_before_marking_complete(tmp_path, media_kind, count):
    item = live_photo_item(count)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=IMAGE_BODY if request.url.path.endswith(".jpg") else VIDEO_BODY)

    state = {}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, media_kind)

    assert result.downloaded == 1 and result.failed == 0
    assert len(result.files) == 2 * count
    assert sum(path.endswith(".jpg") for path in result.files) == count
    assert sum(path.endswith("_live.mp4") for path in result.files) == count
    assert all(request.url.path != "/bgm.mp3" for request in seen)
    assert all(request.headers.get("Cookie") == COOKIE for request in seen)
    state_key = "downloaded_story_ids" if media_kind == "story" else "downloaded_video_ids"
    assert state[state_key] == [item["aweme_id"]]


def test_failed_motion_is_incomplete_and_retry_reuses_saved_still(tmp_path):
    item = live_photo_item()
    state = {}
    seen = []
    fail_motion = True

    def respond(request):
        seen.append(request.url.path)
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=IMAGE_BODY)
        return httpx.Response(503) if fail_motion else httpx.Response(200, content=VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, "video")
        assert first.downloaded == 0 and first.failed == 1
        assert item["aweme_id"] not in state["downloaded_video_ids"]
        assert asset_paths(tmp_path, item)[0].read_bytes() == IMAGE_BODY
        fail_motion = False
        second = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, "video")

    assert second.downloaded == 1 and second.failed == 0
    assert seen.count("/still-0.jpg") == 1
    assert state["downloaded_video_ids"] == [item["aweme_id"]]


def test_previously_completed_still_only_post_gets_missing_motion(tmp_path):
    item = live_photo_item()
    still, motion = asset_paths(tmp_path, item)
    still.parent.mkdir(parents=True)
    still.write_bytes(IMAGE_BODY)
    state = {"downloaded_video_ids": [item["aweme_id"]]}
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, "video")

    assert result.downloaded == 1 and result.failed == 0
    assert seen == ["/motion-0.mp4"]
    assert still.read_bytes() == IMAGE_BODY and motion.read_bytes() == VIDEO_BODY
    assert state["downloaded_video_ids"] == [item["aweme_id"]]


def test_failed_upgrade_clears_completed_id_and_complete_assets_skip(tmp_path):
    item = live_photo_item()
    still, motion = asset_paths(tmp_path, item)
    still.parent.mkdir(parents=True)
    still.write_bytes(IMAGE_BODY)
    state = {"downloaded_video_ids": [item["aweme_id"]]}
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))) as client:
        failed = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, "video")
    assert failed.failed == 1 and failed.downloaded == 0
    assert state["downloaded_video_ids"] == []

    motion.write_bytes(VIDEO_BODY)
    state["downloaded_video_ids"] = [item["aweme_id"]]
    seen = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(503))) as client:
        complete = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, "video")
    assert complete.skipped == 1 and complete.failed == 0
    assert seen == []


def test_nested_motion_redirect_retains_cookie_only_on_trusted_domain(tmp_path):
    item = live_photo_item()
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=IMAGE_BODY)
        if request.url.host == "video-web-cn.douyin.com":
            return httpx.Response(302, headers={"Location": "https://v3-dy-o.zjcdn.com/motion.mp4"})
        return httpx.Response(200, content=VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond), headers={"Cookie": "sessionid=wrong-default"}) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, {}, "video")
    assert result.downloaded == 1 and len(result.files) == 2
    assert any(request.url.host == "v3-dy-o.zjcdn.com" for request in seen)
    assert all(request.headers.get("Cookie") == COOKIE for request in seen if request.url.host == "video-web-cn.douyin.com")
    assert all("Cookie" not in request.headers for request in seen if request.url.host == "v3-dy-o.zjcdn.com")


def test_mixed_album_keeps_still_positions_and_prefers_standard_motion_codec(tmp_path):
    item = live_photo_item(2)
    item["images"][0].pop("video")
    item["images"][1]["video"]["play_addr"] = {"url_list": ["https://video-web-cn.douyin.com/opaque.mp4"]}
    item["images"][1]["video"]["play_addr_h264"] = {"url_list": ["https://video-web-cn.douyin.com/standard.mp4"]}
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=IMAGE_BODY if request.url.path.endswith(".jpg") else VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, {}, "video")
    assert result.downloaded == 1 and len(result.files) == 3
    assert asset_paths(tmp_path, item, index=2)[1].read_bytes() == VIDEO_BODY
    assert seen == ["/still-0.jpg", "/still-1.jpg", "/standard.mp4"]


def test_declared_motion_without_download_url_does_not_mark_complete(tmp_path):
    item = live_photo_item()
    item["images"][0]["video"] = {"duration": 2917}
    state = {}
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=IMAGE_BODY))) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, state, "video")
    assert result.failed == 1 and result.downloaded == 0
    assert item["aweme_id"] not in state["downloaded_video_ids"]


def test_duplicate_live_photo_covers_keep_matching_album_positions(tmp_path):
    item = live_photo_item(2)
    item["images"][1]["url_list"] = item["images"][0]["url_list"][:]

    def respond(request):
        return httpx.Response(200, content=IMAGE_BODY if request.url.path.endswith(".jpg") else VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, {}, "video")
    assert result.downloaded == 1 and len(result.files) == 4
    for index in (1, 2):
        still, motion = asset_paths(tmp_path, item, index=index)
        assert still.read_bytes() == IMAGE_BODY
        assert motion.read_bytes() == VIDEO_BODY


def test_missing_cover_in_mixed_album_does_not_shift_later_live_photo(tmp_path):
    item = live_photo_item(3)
    item["images"][0].pop("video")
    item["images"][1].pop("video")
    item["images"][1]["url_list"] = []

    def respond(request):
        return httpx.Response(200, content=IMAGE_BODY if request.url.path.endswith(".jpg") else VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {"cookies": COOKIE}, [item], tmp_path, {}, "video")
    assert result.downloaded == 1 and len(result.files) == 3
    assert asset_paths(tmp_path, item, index=1)[0].read_bytes() == IMAGE_BODY
    assert not asset_paths(tmp_path, item, index=2)[0].exists()
    still, motion = asset_paths(tmp_path, item, index=3)
    assert still.read_bytes() == IMAGE_BODY and motion.read_bytes() == VIDEO_BODY
