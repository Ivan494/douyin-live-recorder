import httpx
import pytest

from douyin_media_downloader import download_bytes
from security_utils import is_safe_media_download_url, is_safe_share_link_url


CDN = "https://v5-hl-mly-ov.zjcdn.com/video.mp4"


def test_play_api_redirect_to_observed_video_cdn_downloads(tmp_path):
    requested = []
    body = b"\x00\x00\x00\x18ftypisom" + b"x" * 65536

    def respond(request):
        requested.append(request.url.host)
        if request.url.host == "api.amemv.com":
            return httpx.Response(302, headers={"location": CDN})
        return httpx.Response(200, content=body)

    output = tmp_path / "video.mp4"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        download_bytes(client, "https://api.amemv.com/aweme/v1/play/?video_id=fixture", output)
    assert output.read_bytes() == body
    assert requested == ["api.amemv.com", "v5-hl-mly-ov.zjcdn.com"]


@pytest.mark.parametrize("url", [
    "https://other.zjcdn.com/video.mp4",
    "https://v5-hl-mly-ov.zjcdn.com.evil.example/video.mp4",
    "http://127.0.0.1/video.mp4",
    "file:///C:/private.mp4",
])
def test_cdn_exception_does_not_allow_unverified_hosts(url):
    assert not is_safe_media_download_url(url)


def test_video_cdn_is_not_a_share_link_host():
    assert not is_safe_share_link_url(CDN)


def test_later_redirect_from_video_cdn_is_still_validated(tmp_path):
    seen = []

    def respond(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            download_bytes(client, CDN, tmp_path / "video.mp4")
    assert seen == ["v5-hl-mly-ov.zjcdn.com"]
    assert not list(tmp_path.iterdir())
