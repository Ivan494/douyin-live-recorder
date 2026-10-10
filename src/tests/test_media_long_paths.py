"""Exercise real Windows writes, including temporary filename limits."""

import os
from pathlib import Path

import httpx
import pytest

import douyin_media_downloader as media


VIDEO_BODY = b"\x00\x00\x00\x18ftypisom" + b"v" * 4096
IMAGE_BODY = b"\xff\xd8\xff\xe0" + b"i" * 4096


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: "")
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "")


def item(caption):
    return {
        "aweme_id": "7692046175923311247",
        "desc": caption,
        "create_time": 1790944062,
        "video": {"play_addr": {"url_list": ["https://video-web-cn.douyin.com/video.mp4"]}},
    }


def utf16_length(value):
    return len(str(value).encode("utf-16-le")) // 2


def test_full_title_preserves_200_ascii_characters_when_the_component_fits(tmp_path):
    aweme = item("a" * 200)
    output = media.media_item_path(tmp_path, aweme, subdir_by_aweme_id=True, keep_full_title=True)
    assert "a" * 200 in output.name
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=VIDEO_BODY))) as client:
        media.download_bytes(client, "https://video-web-cn.douyin.com/video.mp4", output)
    assert Path(media._extended_path(output)).read_bytes() == VIDEO_BODY


def test_caption_filename_budget_is_stable_across_process_ids(tmp_path, monkeypatch):
    aweme = item("a" * 250)
    monkeypatch.setattr(media.os, "getpid", lambda: 1)
    first = media.media_item_path(tmp_path, aweme, keep_full_title=True)
    monkeypatch.setattr(media.os, "getpid", lambda: 4294967295)
    second = media.media_item_path(tmp_path, aweme, keep_full_title=True)
    assert first == second
    assert utf16_length(first.name + ".json.part.4294967295.12345678") <= 255


@pytest.mark.skipif(os.name != "nt", reason="NTFS filename limits count UTF-16 units")
@pytest.mark.parametrize("as_album", [False, True])
def test_120_emoji_caption_downloads_through_real_ntfs_temporary_file(tmp_path, as_album):
    aweme = item("😀" * 120)
    if as_album:
        aweme["images"] = [{"url_list": ["https://video-web-cn.douyin.com/still.jpg"], "video": aweme["video"]}]
    state = {}

    def respond(request):
        return httpx.Response(200, content=IMAGE_BODY if request.url.path.endswith(".jpg") else VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {}, [aweme], tmp_path, state, "video", keep_full_title=True, save_metadata_json=True)
    assert result.downloaded == 1 and result.failed == 0
    assert len(result.files) == (2 if as_album else 1)
    for filename in result.files:
        output = Path(filename)
        assert utf16_length(output.name + f".part.{os.getpid()}.12345678") <= 255
        assert media._saved_file_exists(output)
    assert media._saved_file_exists(Path(result.files[0] + ".json"))


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended paths")
def test_short_relative_path_under_deep_cwd_is_converted_to_absolute_extended_path(tmp_path, monkeypatch):
    base = tmp_path / ("folder" * 18) / ("directory" * 12)
    output = Path("small.mp4")
    expected = str(base / output)
    assert utf16_length(expected) >= 260
    monkeypatch.setattr(media.os.path, "abspath", lambda _value: expected)
    assert media._extended_path(output) == "\\\\?\\" + expected


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended paths")
def test_long_unc_path_uses_unc_extended_prefix():
    raw = "\\\\server\\share\\" + "folder\\" * 45 + "movie.mp4"
    assert media._extended_path(raw) == "\\\\?\\UNC\\" + raw[2:]


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended paths")
def test_long_download_and_metadata_are_reused_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: "")
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "")
    output = tmp_path / ("folder" * 18) / ("directory" * 12)
    aweme = item("z" * 200)
    state = {}
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=VIDEO_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = media.download_aweme_items(client, {}, [aweme], output, state, "video", save_metadata_json=True)
        second = media.download_aweme_items(client, {}, [aweme], output, {}, "video", save_metadata_json=True)
    assert first.downloaded == 1 and first.failed == 0
    assert second.skipped == 1 and second.failed == 0
    assert calls == ["/video.mp4"]
    assert media._saved_file_exists(Path(first.files[0] + ".json"))


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended paths")
def test_periodic_state_checkpoint_survives_deep_download_root(tmp_path):
    output = tmp_path / ("folder" * 18) / ("directory" * 12)
    items = [{**item("short caption"), "aweme_id": str(index)} for index in range(10)]
    state = {}
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=VIDEO_BODY))) as client:
        result = media.download_aweme_items(client, {}, items, output, state, "video")
    assert result.downloaded == 10 and result.failed == 0
    state_path = output / "douyin_media_state.json"
    _state_path, reloaded = media.load_state(output)
    assert reloaded["downloaded_video_ids"] == [str(index) for index in range(10)]
    assert media._saved_file_exists(state_path)


def test_json_write_failure_preserves_existing_encrypted_payload(tmp_path, monkeypatch):
    output = tmp_path / "session.json"
    existing = b'{"cookie_header_dpapi": "synthetic-protected-payload", "source": "test"}'
    output.write_bytes(existing)

    def interrupted_dump(_data, handle, **_kwargs):
        handle.write("partial serialized content")
        raise OSError("synthetic disk write failure")

    monkeypatch.setattr(media.json, "dump", interrupted_dump)
    with pytest.raises(OSError, match="synthetic disk write failure"):
        media.save_json(output, {"cookie_header_dpapi": "new-synthetic-protected-payload"})
    assert output.read_bytes() == existing
    assert list(tmp_path.iterdir()) == [output]


def test_json_temp_name_collision_preserves_other_writers_file(tmp_path, monkeypatch):
    output = tmp_path / "session.json"
    temporary = output.with_suffix(output.suffix + f".tmp.{os.getpid()}.12345678")
    temporary.write_bytes(b"another writer's in-progress data")
    monkeypatch.setattr(media.uuid, "uuid4", lambda: type("FixedUuid", (), {"hex": "12345678"})())
    with pytest.raises(FileExistsError):
        media.save_json(output, {"cookie_header_dpapi": "synthetic-protected-payload"})
    assert temporary.read_bytes() == b"another writer's in-progress data"
    assert not output.exists()
