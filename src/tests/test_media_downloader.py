import asyncio
import tempfile

import httpx
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import douyin_media_downloader as media


class FakeDouyinClient:
    def __init__(self, session_status=0, post_status=200, post_content=b""):
        self.session_status = session_status
        self.post_status = post_status
        self.post_content = post_content
        self.post_calls = 0
        self.session_checks = 0

    @staticmethod
    def _response(status, *, content=b"", json_data=None, content_type="application/json"):
        request = httpx.Request("GET", "https://www.douyin.com/test")
        if json_data is not None:
            return httpx.Response(status, json=json_data, request=request)
        return httpx.Response(
            status,
            content=content,
            headers={"content-type": content_type},
            request=request,
        )

    def get(self, url, headers=None):
        if url == "https://www.douyin.com/":
            return self._response(200, content=b"ok", content_type="text/html")
        if "/aweme/v1/web/query/user/" in url:
            self.session_checks += 1
            if self.session_status == 0:
                return self._response(200, json_data={"status_code": 0, "id": "account"})
            return self._response(200, json_data={"status_code": self.session_status, "status_msg": "login required"})
        self.post_calls += 1
        return self._response(self.post_status, content=self.post_content)


class MediaDownloaderTest(unittest.TestCase):
    def test_download_profile_uses_anonymous_browser_for_video_list(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = {
                "id": "test",
                "name": "Test",
                "output_dir": temporary_directory,
                "original_profile_url": "https://www.douyin.com/user/test-sec-user",
            }

            with patch.object(media, "apply_saved_session", side_effect=AssertionError("must not use saved session")), patch.object(
                media,
                "fetch_posts",
                side_effect=media.EmptyApiResponseError("http fast-path forced to fail in test"),
            ), patch.object(
                media,
                "fetch_posts_via_mobile_api",
                side_effect=media.EmptyApiResponseError("mobile fallback forced to fail in test"),
            ), patch.object(
                media,
                "fetch_posts_via_browser",
                return_value=[],
            ) as fetch_browser:
                summary = media.download_profile(profile, videos=True, stories=False)

        fetch_browser.assert_called_once()
        self.assertEqual("test-sec-user", fetch_browser.call_args.args[1])
        self.assertEqual("ok", summary["videos"]["status"])

    def test_available_cdp_port_zero_returns_assigned_port(self):
        port = media.available_cdp_port(0)

        self.assertGreater(port, 0)

    def test_profile_identity_uses_saved_profile_url_without_live_request(self):
        sec_uid = "MS4wLjABAAAA-test-profile"
        profile = {
            "name": "Test",
            "url": "https://live.douyin.com/123456",
            "original_profile_url": f"https://www.douyin.com/user/{sec_uid}",
        }

        identity = asyncio.run(media.resolve_profile_identity(profile))

        self.assertEqual(sec_uid, identity["sec_user_id"])
        self.assertEqual("", identity["user_id"])

    def test_live_identity_fallback_never_receives_saved_or_profile_cookies(self):
        profile = {
            "name": "Test",
            "url": "https://live.douyin.com/123456",
            "cookies": "sessionid=secret; uid_tt=secret",
            "stream_orientation": 1,
        }
        live = MagicMock()

        async def fetch_web_stream_data(_url, process_data=True):
            return {"data": {"user": {"sec_uid": "MS4wLjABAAAA-from-live", "id_str": "99", "nickname": "Live"}}}

        live.fetch_web_stream_data = fetch_web_stream_data

        with patch.object(media, "DouyinLiveStream", return_value=live) as live_type:
            identity = asyncio.run(media.resolve_profile_identity(profile))

        self.assertEqual("MS4wLjABAAAA-from-live", identity["sec_user_id"])
        self.assertEqual("99", identity["user_id"])
        self.assertIsNone(live_type.call_args.kwargs["cookies"])

    def test_empty_api_response_with_verified_session_is_neutral_not_antibot(self):
        client = FakeDouyinClient(session_status=0)
        profile = {"cookies": "sessionid=secret"}

        with patch.object(media, "default_query", return_value={"msToken": "token"}), patch.object(
            media, "signed_douyin_url", return_value=("https://www.douyin.com/post", media.USER_AGENT)
        ):
            with self.assertRaises(media.EmptyApiResponseError) as raised:
                media.request_json(client, profile, media.POST_PATH, "sec-user")

        message = str(raised.exception)
        self.assertIn("empty API body", message)
        self.assertIn("cause is unconfirmed", message)
        self.assertNotIn("anti-bot", message.lower())
        self.assertEqual(2, client.post_calls)
        self.assertEqual(1, client.session_checks)

    def test_empty_api_response_with_rejected_session_requests_login(self):
        client = FakeDouyinClient(session_status=12)
        profile = {"cookies": "sessionid=expired"}

        with patch.object(media, "default_query", return_value={"msToken": "token"}), patch.object(
            media, "signed_douyin_url", return_value=("https://www.douyin.com/post", media.USER_AGENT)
        ):
            with self.assertRaises(media.LoginRequiredError):
                media.request_json(client, profile, media.POST_PATH, "sec-user")

        self.assertEqual(1, client.session_checks)

    def test_empty_http_error_is_not_mislabeled_as_login_or_antibot(self):
        client = FakeDouyinClient(session_status=0, post_status=403)
        profile = {"cookies": "sessionid=secret"}

        with patch.object(media, "default_query", return_value={"msToken": "token"}), patch.object(
            media, "signed_douyin_url", return_value=("https://www.douyin.com/post", media.USER_AGENT)
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                media.request_json(client, profile, media.POST_PATH, "sec-user")

        self.assertEqual(1, client.post_calls)
        self.assertEqual(0, client.session_checks)

    def test_empty_non_200_success_is_reported_with_its_actual_status(self):
        client = FakeDouyinClient(session_status=0, post_status=204)
        profile = {"cookies": "sessionid=secret"}

        with patch.object(media, "default_query", return_value={"msToken": "token"}), patch.object(
            media, "signed_douyin_url", return_value=("https://www.douyin.com/post", media.USER_AGENT)
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 204"):
                media.request_json(client, profile, media.POST_PATH, "sec-user")

        self.assertEqual(1, client.post_calls)
        self.assertEqual(0, client.session_checks)

    def test_normalize_items_accepts_familiar_feed_data(self):
        items = [{"aweme_id": "1"}]

        self.assertEqual(items, media.normalize_items({"status_code": 0, "data": items}))

    def test_story_markers_are_detected(self):
        self.assertTrue(media.is_time_limited_story({"is_story": 1}))
        self.assertTrue(media.is_time_limited_story({"story_ttl": 3600}))
        self.assertTrue(media.is_time_limited_story({"moment_info": {"id": "1"}}))
        self.assertFalse(media.is_time_limited_story({"aweme_id": "ordinary"}))

    def test_familiar_feed_filters_author_and_non_story_items(self):
        target = "target-sec-uid"
        payload = {
            "status_code": 0,
            "data": [
                {"aweme_id": "story", "is_story": 1, "author": {"sec_uid": target}},
                {"aweme_id": "normal", "author": {"sec_uid": target}},
                {"aweme_id": "other", "is_story": 1, "author": {"sec_uid": "other"}},
            ],
        }

        with patch.object(media, "request_life_feed", return_value=(None, "life skipped")), patch.object(
            media, "request_json", return_value=payload
        ):
            items, source, supported = media.fetch_stories(object(), {}, target)

        self.assertTrue(supported)
        self.assertEqual(media.FAMILIAR_FEED_PATH, source)
        self.assertEqual(["story"], [item["aweme_id"] for item in items])

    def test_life_feed_stories_are_preferred_over_empty_familiar_feed(self):
        target = "target-sec-uid"
        life_payload = {
            "status_code": 0,
            "user_story_list": [
                {
                    "user": {"sec_uid": target, "uid": "123"},
                    "story_list": [
                        {
                            "aweme_id": "life-story",
                            "is_story": 1,
                            "author": {"sec_uid": target},
                        }
                    ],
                }
            ],
        }
        familiar_payload = {"status_code": 0, "data": []}

        with patch.object(media, "request_life_feed", return_value=(life_payload, media.LIFE_FEED_PATH)), patch.object(
            media, "request_json", return_value=familiar_payload
        ):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertTrue(supported)
        self.assertEqual(media.LIFE_FEED_PATH, source)
        self.assertEqual(["life-story"], [item["aweme_id"] for item in items])

    def test_empty_familiar_feed_does_not_block_later_story_sources(self):
        target = "target-sec-uid"
        familiar_payload = {"status_code": 0, "data": [], "has_more": 1}
        moment_payload = {
            "status_code": 0,
            "aweme_list": [
                {"aweme_id": "moment-story", "is_story": 1, "author": {"sec_uid": target}},
            ],
        }

        def fake_request_json(_client, _profile, path, *_args, **_kwargs):
            if path == media.FAMILIAR_FEED_PATH:
                return familiar_payload
            if path.endswith("moment/list/"):
                return moment_payload
            return {"status_code": 404}

        with patch.object(media, "request_life_feed", return_value=(None, "life empty")), patch.object(
            media, "request_json", side_effect=fake_request_json
        ):
            items, source, supported = media.fetch_stories(object(), {}, target)

        self.assertTrue(supported)
        self.assertIn("moment", source)
        self.assertEqual(["moment-story"], [item["aweme_id"] for item in items])

    def test_active_profile_story_is_not_reported_as_no_active_story(self):
        target = "target-sec-uid"

        def fake_request_json(_client, _profile, path, *_args, **_kwargs):
            if path == "/aweme/v1/web/user/profile/other/":
                return {"status_code": 0, "user": {"story_tab_empty": False}}
            return {"status_code": 0, "data": []}

        with patch.object(
            media,
            "request_life_feed",
            return_value=({"status_code": 0, "user_story_list": None}, "no active visible stories"),
        ), patch.object(media, "request_json", side_effect=fake_request_json):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertEqual([], items)
        self.assertFalse(supported)
        self.assertIn("mobile app", source.lower())

    def test_normalize_items_extracts_user_story_list(self):
        payload = {
            "status_code": 0,
            "user_story_list": [
                {
                    "user": {"sec_uid": "abc"},
                    "all_story_list": [
                        {"aweme_id": "one", "is_story": 1, "author": {"sec_uid": "abc"}},
                    ],
                }
            ],
        }
        items = media.normalize_items(payload)
        self.assertEqual(["one"], [item["aweme_id"] for item in items])

    def test_story_image_urls_are_collected(self):
        aweme = {
            "images": [
                {"url_list": ["https://example.test/one.jpg"]},
                {"display_image": {"url_list": ["https://example.test/two.jpg"]}},
            ]
        }

        self.assertEqual(
            ["https://example.test/one.jpg", "https://example.test/two.jpg"],
            media.collect_image_urls(aweme),
        )

    def test_image_url_collection_selects_one_jpeg_encoding_per_photo(self):
        aweme = {
            "images": [
                {
                    "url_list": [
                        "https://example.test/photo.webp?variant=display",
                        "https://example.test/photo.jpeg?variant=display",
                    ],
                    "download_url_list": [
                        "https://example.test/photo.jpeg?watermark=1"
                    ],
                }
            ]
        }

        self.assertEqual(
            ["https://example.test/photo.jpeg?variant=display"],
            media.collect_image_urls(aweme),
        )

    def test_posted_image_work_downloads_every_image(self):
        aweme = {
            "aweme_id": "image-note",
            "desc": "An image note",
            "images": [
                {"url_list": ["https://example.test/one.jpg"]},
                {"url_list": ["https://example.test/two.jpg"]},
            ],
        }
        state = {}

        def fake_download(_client, url, output_path, progress_callback=None, progress_details=None):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(url.encode("utf-8"))

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, state, "video"
            )
            saved = sorted((Path(temporary_directory) / "images").glob("*.jpg"))

        self.assertEqual(1, result.downloaded)
        self.assertEqual(2, len(saved))
        self.assertEqual(["image-note"], state["downloaded_video_ids"])

    def test_image_work_ignores_bgm_in_video_play_address(self):
        aweme = {
            "aweme_id": "image-with-bgm",
            "aweme_type": 68,
            "duration": 0,
            "video": {
                "duration": 0,
                "play_addr": {
                    "url_list": ["https://example.test/background-music.mp3"]
                },
            },
            "images": [
                {"url_list": ["https://example.test/photo.webp"]},
            ],
        }
        downloaded_urls = []

        def fake_download(_client, url, output_path, progress_callback=None, progress_details=None):
            downloaded_urls.append(url)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"image")

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, {}, "video"
            )
            images = list((Path(temporary_directory) / "images").glob("*.jpg"))
            videos = list((Path(temporary_directory) / "videos").glob("*.mp4"))

        self.assertEqual(["https://example.test/photo.webp"], downloaded_urls)
        self.assertEqual(1, result.downloaded)
        self.assertEqual(1, len(images))
        self.assertEqual([], videos)

    def test_legacy_video_state_is_migrated_without_marking_stories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            media.save_json(root / "douyin_media_state.json", {"downloaded_aweme_ids": ["old-video"]})

            _path, state = media.load_state(root)

        self.assertEqual(["old-video"], state["downloaded_video_ids"])
        self.assertEqual([], state["downloaded_story_ids"])

    def test_response_login_tip_handles_non_numeric_status(self):
        self.assertFalse(media.response_has_login_tip({"status_code": "not-a-number"}))
        self.assertTrue(
            media.response_has_login_tip(
                {"status_code": 0, "not_login_module": {"guide_login_tip_exist": True}}
            )
        )

    def test_login_promotion_does_not_invalidate_public_video_items(self):
        payload = {
            "status_code": 0,
            "aweme_list": [{"aweme_id": "public-video"}],
            "not_login_module": {"guide_login_tip_exist": True},
            "has_more": 0,
        }

        self.assertTrue(media.response_has_login_tip(payload))
        self.assertEqual(["public-video"], [item["aweme_id"] for item in media.normalize_items(payload)])

    def test_cdp_probe_bypasses_environment_proxy(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/test"}
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response

        with patch.object(media.httpx, "Client", return_value=client) as client_type:
            available = media.cdp_is_available("http://127.0.0.1:9223")

        self.assertTrue(available)
        client_type.assert_called_once_with(trust_env=False)
        client.get.assert_called_once_with("http://127.0.0.1:9223/json/version", timeout=2)

    def test_media_browser_info_requires_edge_and_bypasses_proxy(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"Browser": "Edg/150.0.4078.83"}
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response

        with patch.object(media.httpx, "Client", return_value=client) as client_type:
            info = media._require_edge_browser("http://127.0.0.1:9344")

        self.assertEqual("150.0.4078.83", info["version"])
        client_type.assert_called_once_with(trust_env=False)
        client.get.assert_called_once_with("http://127.0.0.1:9344/json/version", timeout=5)

    def test_media_browser_rejects_chrome(self):
        with patch.object(
            media,
            "_cdp_browser_info",
            return_value={"browser": "Chrome/150.0.7871.186", "product": "Chrome", "version": "150.0.7871.186"},
        ):
            with self.assertRaisesRegex(RuntimeError, "requires Microsoft Edge"):
                media._require_edge_browser("http://127.0.0.1:9344")

    def test_media_browser_override_is_honored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "msedge.exe"
            executable.touch()
            with patch.dict(media.os.environ, {"DOUYIN_MEDIA_EDGE_PATH": str(executable)}):
                selected = media.find_media_browser_executable()

        self.assertEqual(executable, selected)

    def test_login_browser_uses_media_edge_and_fetch_profile(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "msedge.exe"
            profile_dir = root / "fetch-profile"
            process = MagicMock()
            process.poll.return_value = None

            with patch.object(
                media, "find_media_browser_executable", return_value=executable
            ) as find_browser, patch.object(
                media, "FETCH_BROWSER_PROFILE_DIR", profile_dir
            ), patch.object(
                media, "cdp_is_available", side_effect=[True, True]
            ), patch.object(
                media, "close_cdp_browser"
            ) as close_browser, patch.object(
                media, "FETCH_BROWSER_CDP_PORT", 9456
            ), patch.object(
                media.subprocess, "Popen", return_value=process
            ) as popen:
                launched = media.launch_douyin_login_browser()
                self.assertTrue(profile_dir.is_dir())

        find_browser.assert_called_once_with()
        close_browser.assert_called_once_with("http://127.0.0.1:9456")
        command = popen.call_args.args[0]
        self.assertEqual(str(executable), command[0])
        self.assertIn("--remote-debugging-port=9456", command)
        self.assertIn(f"--user-data-dir={profile_dir}", command)
        self.assertEqual("http://127.0.0.1:9456", launched["cdp_url"])
        self.assertEqual(str(profile_dir), launched["profile_dir"])
        self.assertIs(process, launched["process"])

    def test_close_cdp_browser_uses_browser_websocket_and_waits_for_shutdown(self):
        response = MagicMock()
        response.json.return_value = {
            "webSocketDebuggerUrl": "ws://127.0.0.1:9344/devtools/browser/test"
        }
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response

        with patch.object(media.httpx, "Client", return_value=client), patch.object(
            media, "chrome_cdp_command", side_effect=RuntimeError("connection closed")
        ) as command, patch.object(media, "cdp_is_available", return_value=False):
            media.close_cdp_browser("http://127.0.0.1:9344")

        command.assert_called_once_with(
            "ws://127.0.0.1:9344/devtools/browser/test",
            "Browser.close",
            timeout=3,
        )

    def test_fetch_posts_reports_each_history_page(self):
        payloads = [
            {
                "aweme_list": [{"aweme_id": "one"}],
                "has_more": 1,
                "max_cursor": 10,
            },
            {
                "aweme_list": [{"aweme_id": "two"}],
                "has_more": 0,
                "max_cursor": 10,
            },
        ]
        progress = []

        with patch.object(media, "request_json", side_effect=payloads):
            items = media.fetch_posts(
                object(),
                {},
                "test-sec-user",
                progress_callback=progress.append,
            )

        self.assertEqual(["one", "two"], [item["aweme_id"] for item in items])
        self.assertEqual(
            [(1, 1), (2, 2)],
            [(event["pages"], event["found"]) for event in progress],
        )

    def test_download_items_reports_item_and_completion_progress(self):
        item = {
            "aweme_id": "new-video",
            "desc": "A visible progress item",
            "video": {"play_addr": {"url_list": ["https://example.test/video.mp4"]}},
        }
        progress = []

        def fake_download(_client, _url, output_path, progress_callback=None, progress_details=None):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"video")
            media.report_progress(
                progress_callback,
                phase="downloading",
                bytes_downloaded=5,
                bytes_total=5,
                **(progress_details or {}),
            )

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            result = media.download_aweme_items(
                object(),
                {},
                [item],
                temporary_directory,
                {},
                "video",
                progress_callback=progress.append,
            )

        self.assertEqual(1, result.downloaded)
        self.assertTrue(any(event.get("bytes_downloaded") == 5 for event in progress))
        self.assertEqual(1, progress[-1]["downloaded"])
        self.assertEqual("A visible progress item", progress[-1]["item"])


if __name__ == "__main__":
    unittest.main()
