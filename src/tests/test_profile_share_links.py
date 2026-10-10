from unittest.mock import patch

import httpx
import pytest

import douyin_media_downloader as media
import douyin_recorder_app as app


@pytest.mark.parametrize("url", [
    "https://evildouyin.com/user/author",
    "https://www.douyin.com.evil.example/user/author",
    "https://evil.example/?next=https://www.douyin.com/user/author",
    "https://user:secret@www.douyin.com/user/author",
    "https://www.douyin.com/user/author%2Fother",
])
def test_profile_url_rejects_untrusted_or_ambiguous_urls(url):
    assert media.extract_sec_uid_from_url(url) == ""
    assert media.canonical_douyin_profile_url(url) == ""


@pytest.mark.parametrize("text", [
    "mail@v.douyin.com/author/",
    "evil.v.douyin.com/author/",
    "https://www.douyin.com.evil.example/user/author",
    "https://user:secret@v.douyin.com/author/",
])
def test_share_blob_does_not_extract_email_or_lookalike_host(text):
    assert media.normalize_pasted_link(text) == ""


def test_share_profile_and_live_platforms_use_hostname():
    assert app.detect_platform("https://www.iesdouyin.com/share/user/author") == "douyin"
    assert app.detect_platform("https://www.douyin.com.evil.example/user/author") == "unknown"
    assert app.detect_platform("https://evil.example/?x=youtube.com") == "unknown"
    assert app.detect_platform("https://m.youtube.com/@author/live") == "youtube"
    assert app.detect_platform("https://youtu.be/abc") == "youtube"


def test_expansion_avoids_http_for_a_concrete_live_url():
    url = "https://live.douyin.com/1234"
    with patch.object(media.httpx, "Client", side_effect=AssertionError("Concrete links need no HTTP")):
        assert media.expand_douyin_short_link(url) == url


def test_expansion_reports_failure_without_echoing_sensitive_url():
    with patch.object(media.httpx, "Client", side_effect=RuntimeError("secret-token")):
        with pytest.raises(RuntimeError, match="Could not resolve") as error:
            media.expand_douyin_short_link("https://v.douyin.com/author/")
    assert "secret-token" not in str(error.value)


def test_short_link_redirects_are_public_and_return_canonical_profile():
    requests = []
    client_factory = httpx.Client

    def respond(request):
        requests.append(request)
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={
                "location": "https://www.douyin.com/user/author?from=share",
                "set-cookie": "sessionid=private; Domain=.douyin.com; Path=/",
            })
        return httpx.Response(200)

    transport = httpx.MockTransport(respond)
    with patch.object(media.httpx, "Client", side_effect=lambda **kwargs: client_factory(transport=transport, **kwargs)):
        expanded = media.expand_douyin_short_link("https://v.douyin.com/author/")
    assert media.canonical_douyin_profile_url(expanded) == "https://www.douyin.com/user/author"
    assert len(requests) == 2
    assert all("cookie" not in request.headers for request in requests)


def test_short_link_cannot_redirect_to_an_untrusted_or_credential_url():
    client_factory = httpx.Client
    for destination in ("https://www.douyin.com.evil.example/user/author", "https://user:secret@www.douyin.com/user/author"):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": destination})

        transport = httpx.MockTransport(respond)
        with patch.object(media.httpx, "Client", side_effect=lambda **kwargs: client_factory(transport=transport, **kwargs)):
            with pytest.raises(RuntimeError, match="Could not resolve") as error:
                media.expand_douyin_short_link("https://v.douyin.com/author/")
        assert len(requests) == 1
        assert destination not in str(error.value)
