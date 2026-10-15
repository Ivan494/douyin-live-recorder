"""Uploaded receipts and stable item markers must not hide incomplete media."""
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import douyin_media_downloader as media

ITEM_ID = "7696544512231551040"


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Network access is forbidden in dedupe tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(media, "_profile_cookie_header", lambda _profile: "")
    monkeypatch.setenv(media.UPLOADED_LOG_ENV, str(tmp_path / "uploaded.jsonl"))


@pytest.fixture
def item():
    return {"aweme_id": ITEM_ID, "create_time": 1760426614, "desc": "new caption",
            "video": {"play_addr": {"url_list": ["https://example.test/media.mp4"]}}}


def write_log(entries):
    path = Path(os.environ[media.UPLOADED_LOG_ENV])
    path.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")
    return path


def test_corrupt_jsonl_records_do_not_hide_later_valid_receipts(tmp_path):
    log = write_log([{"item_id": "first"}, None, [], 42, "string", {"item_id": {"bad": 1}},
                     {"item_id": "last"}, {"path": "no-id"}])
    with log.open("ab") as handle:
        handle.write(b'\xff\n{"item_id":"after-invalid-utf8"}\n{"item_id":"torn')
    assert media._uploaded_aweme_ids(tmp_path, {"uploaded_aweme_ids": ["state-id"]}) == {
        "first", "last", "after-invalid-utf8", "state-id"}


def test_uploaded_id_skip_does_not_call_downloader(tmp_path, monkeypatch, item):
    write_log([{"item_id": ITEM_ID}, {"item_id": ITEM_ID}])
    download = Mock(side_effect=AssertionError("already uploaded"))
    monkeypatch.setattr(media, "download_bytes", download)
    state = {}
    result = media.download_aweme_items(object(), {}, [item], tmp_path, state, "story")
    assert (result.skipped, result.downloaded, result.failed) == (1, 0, 0)
    assert state["downloaded_story_ids"] == [ITEM_ID]
    download.assert_not_called()


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("ending", ["_old caption.mp4", ".mp4"])
def test_old_caption_and_timezone_markers_dedupe_valid_video(tmp_path, monkeypatch, item, grouped, ending):
    target = tmp_path / "stories"
    root = target / ITEM_ID if grouped else target
    root.mkdir(parents=True)
    old = root / f"2025-10-14_07-23-34_{ITEM_ID}{ending}"
    old.write_bytes(b"valid fixture")
    monkeypatch.setattr(media, "_has_valid_media_file", lambda p: Path(p).is_file() and Path(p).stat().st_size > 0)
    found = media._already_saved_media_file(media.media_item_path(target, item, subdir_by_aweme_id=grouped),
                                          [], target, item, subdir_by_aweme_id=grouped)
    assert found == old


@pytest.mark.parametrize("name", [f"stamp_{ITEM_ID}9_old.mp4", f"stamp_{ITEM_ID}_old.mp4.part.123.abc",
    f"stamp_{ITEM_ID}_old.part.mp4", f"stamp_{ITEM_ID}_old.mp4.json", f"stamp_{ITEM_ID}_zero.mp4"])
def test_partial_wrong_id_metadata_and_empty_files_do_not_dedupe(tmp_path, item, name):
    path = tmp_path / name
    path.write_bytes(b"" if "zero" in name else b"invalid media")
    assert media._already_saved_media_file(media.media_item_path(tmp_path, item), [], tmp_path, item) is None


@pytest.mark.parametrize("suffix", [".mp4.json", "_01.jpg", "_01_live.mp4"])
def test_single_asset_receipt_cannot_mark_whole_item_uploaded(tmp_path, suffix):
    write_log([{"item_id": ITEM_ID, "path": str(tmp_path / f"stamp_{ITEM_ID}{suffix}")}])
    assert ITEM_ID not in media._uploaded_aweme_ids(tmp_path)


def album_item(item):
    return {**item, "images": [{"url_list": ["https://example.test/one.jpg"]},
                               {"url_list": ["https://example.test/two.jpg"]}]}


@pytest.mark.parametrize("uploaded_count", [1, 2])
@pytest.mark.parametrize("grouped", [False, True])
def test_album_offload_dedupes_only_when_all_assets_exist(tmp_path, monkeypatch, item, uploaded_count, grouped):
    item = album_item(item)
    target = tmp_path / "stories"
    stem = media.media_item_path(target, item, suffix="", subdir_by_aweme_id=grouped)
    paths = [Path(f"{stem}_{i:02}.jpg") for i in (1, 2)]
    write_log([{"item_id": ITEM_ID, "path": str(p)} for p in paths[:uploaded_count]])
    downloaded = []
    def download(_client, _url, path, **_kwargs):
        downloaded.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"valid fixture")
    monkeypatch.setattr(media, "download_bytes", download)
    monkeypatch.setattr(media, "_has_valid_media_file", lambda p: Path(p).is_file())
    result = media.download_aweme_items(object(), {}, [item], tmp_path, {}, "story", subdir_by_aweme_id=grouped)
    assert result.failed == 0
    if uploaded_count == 1:
        assert result.downloaded == 1 and result.skipped == 0
        assert downloaded == [paths[1]]
    else:
        assert result.skipped == 1 and downloaded == []


def test_old_caption_album_needs_every_cover_and_motion(tmp_path, monkeypatch, item):
    item = album_item(item)
    item["images"][0]["video"] = {"play_addr": {"url_list": ["https://example.test/motion.mp4"]}}
    target = tmp_path / "stories"
    target.mkdir()
    stem = target / f"old-time_{ITEM_ID}_old caption"
    paths = [Path(f"{stem}_{i:02}.jpg") for i in (1, 2)]
    for path in paths:
        path.write_bytes(b"valid fixture")
    monkeypatch.setattr(media, "_has_valid_media_file", lambda p: Path(p).is_file())
    output = media.media_item_path(target, item)
    image_urls = media.collect_image_urls(item)
    assert media._already_saved_media_file(output, image_urls, target, item) is None
    motion = Path(f"{stem}_01_live.mp4")
    motion.write_bytes(b"valid fixture")
    assert media._already_saved_media_file(output, image_urls, target, item) == paths[0]


def test_partial_album_offload_with_edited_caption_reuses_uploaded_asset(tmp_path, monkeypatch, item):
    item = album_item(item)
    target = tmp_path / "stories"
    old = target / f"old-time_{ITEM_ID}_old caption_01.jpg"
    write_log([{"item_id": ITEM_ID, "path": str(old)}])
    downloaded = []
    def download(_client, _url, path, **_kwargs):
        downloaded.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"valid fixture")
    monkeypatch.setattr(media, "download_bytes", download)
    monkeypatch.setattr(media, "_has_valid_media_file", lambda p: Path(p).is_file())
    state = {}
    result = media.download_aweme_items(object(), {}, [item], tmp_path, state, "story")
    assert result.downloaded == 1 and result.failed == 0
    assert len(downloaded) == 1 and downloaded[0].name.endswith("_02.jpg")
    # The next cycle must see the remote cover and the newly saved local cover.
    result = media.download_aweme_items(object(), {}, [item], tmp_path, state, "story")
    assert result.skipped == 1 and len(downloaded) == 1


def test_whole_video_receipt_cannot_hide_new_album_assets(tmp_path, monkeypatch, item):
    item = album_item(item)
    write_log([{"item_id": ITEM_ID, "path": str(tmp_path / f"stamp_{ITEM_ID}_video.mp4")}])
    download = Mock(side_effect=OSError("synthetic download failure"))
    monkeypatch.setattr(media, "download_bytes", download)
    result = media.download_aweme_items(object(), {}, [item], tmp_path, {}, "story")
    assert result.failed == 1 and result.skipped == 0
    assert download.call_count == 2


def test_full_old_caption_album_can_mix_local_and_offloaded_assets(tmp_path, monkeypatch, item):
    item = album_item(item)
    target = tmp_path / "stories"
    target.mkdir()
    old = target / f"old-time_{ITEM_ID}_old caption"
    first, second = Path(f"{old}_01.jpg"), Path(f"{old}_02.jpg")
    second.write_bytes(b"valid fixture")
    write_log([{"item_id": ITEM_ID, "path": str(first)}])
    monkeypatch.setattr(media, "_has_valid_media_file", lambda p: Path(p).is_file())
    download = Mock(side_effect=AssertionError("complete album"))
    monkeypatch.setattr(media, "download_bytes", download)
    result = media.download_aweme_items(object(), {}, [item], tmp_path, {}, "story")
    assert result.skipped == 1 and result.failed == 0
    download.assert_not_called()


@pytest.mark.parametrize("grouped", [False, True])
def test_valid_looking_staging_file_never_dedupes_item(tmp_path, item, grouped):
    target = tmp_path / ITEM_ID if grouped else tmp_path
    target.mkdir(exist_ok=True)
    partial = target / f"stamp_{ITEM_ID}_caption.part.mp4"
    partial.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"v" * 4096)
    assert media._has_valid_media_file(partial)
    output = media.media_item_path(tmp_path, item, subdir_by_aweme_id=grouped)
    assert media._already_saved_media_file(output, [], tmp_path, item, subdir_by_aweme_id=grouped) is None


def test_offloaded_cover_does_not_hide_missing_live_photo_motion(tmp_path, monkeypatch, item):
    item = album_item(item)
    item["images"] = item["images"][:1]
    item["images"][0]["video"] = {"play_addr": {"url_list": ["https://example.test/motion.mp4"]}}
    stem = media.media_item_path(tmp_path / "stories", item, suffix="")
    write_log([{"item_id": ITEM_ID, "path": f"{stem}_01.jpg"}])
    downloaded = []
    def download(_client, _url, path, **_kwargs):
        downloaded.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"valid fixture")
    monkeypatch.setattr(media, "download_bytes", download)
    monkeypatch.setattr(media, "_has_valid_media_file", lambda p: Path(p).is_file())
    result = media.download_aweme_items(object(), {}, [item], tmp_path, {}, "story")
    assert result.downloaded == 1 and result.failed == 0
    assert downloaded == [Path(f"{stem}_01_live.mp4")]
