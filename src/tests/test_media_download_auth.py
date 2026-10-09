"""Media authentication at the real item/download boundaries; HTTP is synthetic."""

from pathlib import Path

import httpx
import pytest

import douyin_media_downloader as media


CLIENT = httpx.Client
PROFILE_COOKIE = "sessionid=synthetic-profile"
SAVED_COOKIE = "sessionid=synthetic-saved-mobile"
DEFAULT_COOKIE = "sessionid=synthetic-client-default"
VIDEO_BODY = b"\x00\x00\x00\x18ftypisom" + b"x" * 4096
IMAGE_BODY = b"\xff\xd8\xff\xe0" + b"x" * 4096
MEDIA_URL = "https://video-web-cn.douyin.com/media.mp4?video_id=fixture123"


@pytest.fixture(autouse=True)
def synthetic_sessions(monkeypatch):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: SAVED_COOKIE)
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "sessionid=synthetic-web")

    def offline_client(*args, **kwargs):
        # Failure paths can construct a fresh fallback client; none may use the network.
        kwargs.setdefault("transport", httpx.MockTransport(lambda request: httpx.Response(403)))
        return CLIENT(*args, **kwargs)

    monkeypatch.setattr(media.httpx, "Client", offline_client)


def item_fixture(format_kind, url=MEDIA_URL):
    item = {"aweme_id": "1234567890123456789", "desc": "synthetic friends item",
            "private_status": 2, "create_time": 1}
    if format_kind == "image":
        item["images"] = [{"url_list": [url]}]
    else:
        item["video"] = {"play_addr": {"url_list": [url]}}
    return item


def default_cookie_kwargs(source):
    if source == "header":
        return {"headers": {"Cookie": DEFAULT_COOKIE}}
    return {"cookies": {"sessionid": "synthetic-client-default"}}


@pytest.mark.parametrize("media_kind", ["story", "video"])
@pytest.mark.parametrize("format_kind", ["video", "image"])
@pytest.mark.parametrize("cookie_source", ["profile", "saved"])
def test_friends_only_media_downloads_with_selected_account(
        tmp_path, media_kind, format_kind, cookie_source):
    expected_cookie = PROFILE_COOKIE if cookie_source == "profile" else SAVED_COOKIE
    profile = {"cookies": f"  {PROFILE_COOKIE}  " if cookie_source == "profile" else "  "}
    body = IMAGE_BODY if format_kind == "image" else VIDEO_BODY
    seen = []

    def respond(request):
        seen.append(request.headers.get("Cookie"))
        if request.headers.get("Cookie") != expected_cookie:
            return httpx.Response(403)
        return httpx.Response(200, content=body)

    state = {}
    with CLIENT(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(
            client, profile, [item_fixture(format_kind)], tmp_path, state, media_kind)

    assert seen and all(cookie == expected_cookie for cookie in seen)
    assert result.downloaded == 1 and result.failed == 0
    assert len(result.files) == 1
    assert Path(result.files[0]).read_bytes() == body
    state_key = "downloaded_story_ids" if media_kind == "story" else "downloaded_video_ids"
    assert state[state_key] == ["1234567890123456789"]


@pytest.mark.parametrize("media_kind", ["story", "video"])
@pytest.mark.parametrize("cookie_source", ["profile", "saved"])
def test_fresh_play_api_fallback_retains_selected_account(
        tmp_path, monkeypatch, media_kind, cookie_source):
    expected_cookie = PROFILE_COOKIE if cookie_source == "profile" else SAVED_COOKIE
    profile = {"cookies": PROFILE_COOKIE if cookie_source == "profile" else ""}
    primary_requests = []
    fallback_requests = []

    def fail_cdn(request):
        primary_requests.append(request)
        return httpx.Response(503)

    def fallback_response(request):
        fallback_requests.append(request)
        if request.headers.get("Cookie") != expected_cookie:
            return httpx.Response(403)
        return httpx.Response(200, content=VIDEO_BODY)

    def fresh_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fallback_response)
        return CLIENT(*args, **kwargs)

    monkeypatch.setattr(media.httpx, "Client", fresh_client)
    with CLIENT(transport=httpx.MockTransport(fail_cdn)) as client:
        result = media.download_aweme_items(
            client, profile, [item_fixture("video")], tmp_path, {}, media_kind)

    assert primary_requests
    assert fallback_requests and fallback_requests[0].url.host == "api.amemv.com"
    assert fallback_requests[0].url.params["video_id"] == "fixture123"
    assert fallback_requests[0].headers.get("Cookie") == expected_cookie
    assert result.downloaded == 1 and result.failed == 0
    assert Path(result.files[0]).read_bytes() == VIDEO_BODY


@pytest.mark.parametrize("media_kind", ["story", "video"])
@pytest.mark.parametrize("format_kind", ["video", "image"])
def test_public_media_downloads_without_saved_login(
        tmp_path, monkeypatch, media_kind, format_kind):
    monkeypatch.setattr(media, "load_mobile_session_cookie_header", lambda: "")
    monkeypatch.setattr(media, "load_session_cookie_header", lambda: "")
    body = IMAGE_BODY if format_kind == "image" else VIDEO_BODY
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=body)

    item = item_fixture(format_kind)
    item["private_status"] = 0
    with CLIENT(transport=httpx.MockTransport(respond)) as client:
        result = media.download_aweme_items(client, {}, [item], tmp_path, {}, media_kind)

    assert result.downloaded == 1 and result.failed == 0
    assert requests and all("Cookie" not in request.headers for request in requests)
    assert Path(result.files[0]).read_bytes() == body


@pytest.mark.parametrize("origin", [
    "https://video-web-cn.douyin.com/start.mp4",
    "https://api.amemv.com/start.mp4",
])
@pytest.mark.parametrize("target, may_send_cookie, may_request", [
    ("https://aweme.snssdk.com/end.mp4", True, True),
    ("https://www.iesdouyin.com:443/end.mp4", True, True),
    ("https://v3-dy-o.zjcdn.com/end.mp4", False, True),
    ("http://api.amemv.com/end.mp4", False, True),
    ("https://api.amemv.com:8443/end.mp4", False, True),
    ("https://user:pass@api.amemv.com/end.mp4", False, True),
    ("https://api.amemv.com.evil.example/end.mp4", False, False),
])
@pytest.mark.parametrize("default_cookie_source", ["header", "jar"])
def test_each_redirect_hop_limits_cookie_to_authenticated_media_domains(
        tmp_path, origin, target, may_send_cookie, may_request, default_cookie_source):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=VIDEO_BODY)

    output = tmp_path / "video.mp4"
    with CLIENT(transport=httpx.MockTransport(respond),
                **default_cookie_kwargs(default_cookie_source)) as client:
        if may_request:
            media.download_bytes(client, origin, output,
                                 extra_headers={"Cookie": PROFILE_COOKIE})
        else:
            with pytest.raises(ValueError):
                media.download_bytes(client, origin, output,
                                     extra_headers={"Cookie": PROFILE_COOKIE})

    assert requests[0].headers.get("Cookie") == PROFILE_COOKIE
    if may_request:
        assert len(requests) == 2
        if may_send_cookie:
            assert requests[1].headers.get("Cookie") == PROFILE_COOKIE
        else:
            assert "Cookie" not in requests[1].headers
        assert output.read_bytes() == VIDEO_BODY
    else:
        assert len(requests) == 1
        assert not output.exists()
    assert not list(tmp_path.glob("*.part.*"))


@pytest.mark.parametrize("url", [
    "https://v3-dy-o.zjcdn.com/video.mp4",
    "http://api.amemv.com/video.mp4",
    "https://api.amemv.com:8443/video.mp4",
    "https://user:pass@api.amemv.com/video.mp4",
])
@pytest.mark.parametrize("default_cookie_source", ["header", "jar"])
def test_initial_media_request_removes_merged_client_cookie_outside_auth_domains(
        tmp_path, url, default_cookie_source):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=VIDEO_BODY)

    output = tmp_path / "video.mp4"
    with CLIENT(transport=httpx.MockTransport(respond),
                **default_cookie_kwargs(default_cookie_source)) as client:
        media.download_bytes(client, url, output)

    assert requests and "Cookie" not in requests[0].headers
    assert output.read_bytes() == VIDEO_BODY


@pytest.mark.parametrize("route", ["posts", "posts_retry", "stories"])
def test_download_profile_uses_same_selected_account_for_metadata_and_binary(
        tmp_path, monkeypatch, route):
    sec_uid = "synthetic-author-sec-uid"
    item = item_fixture("video")
    item["author"] = {"sec_uid": sec_uid}
    if route == "stories":
        item["is_story"] = 1
    profile = {"id": "synthetic-profile", "name": "synthetic profile",
               "cookies": f"  {PROFILE_COOKIE}  ", "output_dir": str(tmp_path)}
    metadata_cookies = []
    requests = []

    async def identity(_profile):
        return {"sec_user_id": sec_uid, "user_id": "42", "nickname": "synthetic"}

    def posts(_client, _sec_uid, *, limit=0, cookie_header=""):
        metadata_cookies.append(cookie_header)
        if route == "posts_retry" and len(metadata_cookies) == 1:
            raise media.EmptyApiResponseError("synthetic initial metadata failure")
        return [item]

    def stories(_client, _sec_uid, cookie_header=""):
        metadata_cookies.append(cookie_header)
        return [item], "synthetic story metadata"

    def unavailable(*args, **kwargs):
        raise media.EmptyApiResponseError("synthetic metadata route unavailable")

    def binary(request):
        requests.append(request)
        return httpx.Response(200 if request.headers.get("Cookie") == PROFILE_COOKIE else 403,
                              content=VIDEO_BODY)

    def offline_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(binary)
        return CLIENT(*args, **kwargs)

    monkeypatch.setattr(media, "resolve_profile_identity", identity)
    monkeypatch.setattr(media, "fetch_posts_via_mobile_api", posts)
    monkeypatch.setattr(media, "fetch_stories_via_mobile_post_api", stories)
    monkeypatch.setattr(media, "fetch_posts", unavailable)
    monkeypatch.setattr(media, "fetch_posts_via_browser", unavailable)
    monkeypatch.setattr(media, "_http_fastpath_failures", {})
    monkeypatch.setattr(media.httpx, "Client", offline_client)

    summary = media.download_profile(profile, videos=route != "stories", stories=route == "stories")

    assert metadata_cookies == [PROFILE_COOKIE] * (2 if route == "posts_retry" else 1)
    result = summary["stories" if route == "stories" else "videos"]
    assert result["status"] == "ok" and result["downloaded"] == 1 and result["failed"] == 0
    assert requests and all(request.headers.get("Cookie") == PROFILE_COOKIE for request in requests)
    assert Path(result["files"][0]).read_bytes() == VIDEO_BODY


def test_story_metadata_wrapper_prefers_explicit_profile_account(monkeypatch):
    sec_uid = "synthetic-author-sec-uid"
    item = item_fixture("video")
    item.update({"is_story": 1, "author": {"sec_uid": sec_uid}})
    cookies = []

    def story_metadata(_client, _sec_uid, cookie_header=""):
        cookies.append(cookie_header)
        return [item], "synthetic story metadata"

    monkeypatch.setattr(media, "fetch_stories_via_mobile_post_api", story_metadata)
    with CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
        items, _, supported = media.fetch_stories(
            client, {"cookies": f"  {PROFILE_COOKIE}  "}, sec_uid)

    assert cookies == [PROFILE_COOKIE]
    assert supported and items == [item]
