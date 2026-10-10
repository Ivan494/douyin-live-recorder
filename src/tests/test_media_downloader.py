import asyncio
import json
import os
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

    def test_download_profile_prefers_app_login_mobile_posts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = {
                "id": "test",
                "name": "Test",
                "output_dir": temporary_directory,
                "original_profile_url": "https://www.douyin.com/user/test-sec-user",
                "cookies": "sessionid=secret",
            }
            posts = [{"aweme_id": "app-post", "desc": "from app"}]

            with patch.object(media, "apply_saved_session", side_effect=AssertionError("must not mutate profile")), patch.object(
                media, "_mobile_cookie_header", return_value="sessionid=secret"
            ), patch.object(
                media, "fetch_posts_via_mobile_api", return_value=posts
            ) as fetch_mobile, patch.object(
                media, "fetch_posts", side_effect=AssertionError("web post must not run after app login")
            ), patch.object(
                media, "fetch_posts_via_browser", side_effect=AssertionError("browser must not run after app login")
            ), patch.object(
                media, "download_aweme_items", return_value=media.MediaResult(status="ok", downloaded=1)
            ):
                summary = media.download_profile(profile, videos=True, stories=False)

        fetch_mobile.assert_called_once()
        self.assertEqual("test-sec-user", fetch_mobile.call_args.args[1])
        self.assertEqual("sessionid=secret", fetch_mobile.call_args.kwargs["cookie_header"])
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

        with patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")), patch.object(
            media, "fetch_stories_via_mobile_story_feed", return_value=(None, "no feed")
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(media, "request_life_feed", return_value=(None, "life skipped")), patch.object(
            media, "request_json", return_value=payload
        ), patch.object(
            media, "fetch_stories_via_emulator", return_value=(None, "no emu")
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

        with patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")), patch.object(
            media, "fetch_stories_via_mobile_story_feed", return_value=(None, "no feed")
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(media, "request_life_feed", return_value=(life_payload, media.LIFE_FEED_PATH)), patch.object(
            media, "request_json", return_value=familiar_payload
        ), patch.object(
            media, "fetch_stories_via_emulator", return_value=(None, "no emu")
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

        with patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")), patch.object(
            media, "fetch_stories_via_mobile_story_feed", return_value=(None, "no feed")
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(media, "request_life_feed", return_value=(None, "life empty")), patch.object(
            media, "request_json", side_effect=fake_request_json
        ), patch.object(
            media, "fetch_stories_via_emulator", return_value=(None, "no emu")
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

        with patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")), patch.object(
            media, "fetch_stories_via_mobile_story_feed",
            return_value=([], "https://aweme.snssdk.com/aweme/v1/story/profile/list/: empty pack"),
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(
            media,
            "request_life_feed",
            return_value=({"status_code": 0, "user_story_list": None}, "no active visible stories"),
        ), patch.object(media, "request_json", side_effect=fake_request_json), patch.object(
            media, "fetch_stories_via_emulator", return_value=(None, "no emu")
        ):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertEqual([], items)
        self.assertTrue(supported)
        self.assertIn("story/profile/list", source)
        self.assertIn("empty pack", source.lower())

    def test_historical_image_notes_do_not_mask_active_story_ring(self):
        target = "target-sec-uid"
        old_note = {
            "aweme_id": "old-image-note",
            "aweme_type": 68,
            "is_story": 0,
            "is_25_story": 0,
            "author": {"sec_uid": target},
        }

        def fake_request_json(_client, _profile, path, *_args, **_kwargs):
            if path == "/aweme/v1/web/user/profile/other/":
                return {"status_code": 0, "user": {"story_tab_empty": False}}
            return {"status_code": 0, "data": []}

        with patch.object(
            media,
            "fetch_stories_via_mobile_post_api",
            return_value=([old_note], "https://aweme.snssdk.com/aweme/v1/aweme/post/ (mobile, 1 stories)"),
        ), patch.object(
            media, "fetch_stories_via_mobile_story_feed",
            return_value=([], "https://aweme.snssdk.com/aweme/v1/story/profile/list/: empty pack"),
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(
            media,
            "request_life_feed",
            return_value=({"status_code": 0, "user_story_list": None}, "no active visible stories"),
        ), patch.object(media, "request_json", side_effect=fake_request_json), patch.object(
            media, "fetch_stories_via_emulator", return_value=(None, "no emu")
        ):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertEqual([], items)
        self.assertTrue(supported)
        self.assertIn("story/profile/list", source)
        self.assertIn("empty pack", source.lower())

    def test_empty_profile_list_and_story_feed_means_no_stories(self):
        # profile/list 只覆盖 TTL 内的日常，历史日常在 story/feed 里，
        # 所以"没有日常"必须两边都问过才能下结论。
        target = "target-sec-uid"
        payload = {
            "status_code": 0,
            "active_data": {"data": None, "has_more": False},
            "month_list": [],
        }
        response = MagicMock()
        response.json.return_value = payload
        seen = []

        def fake_request(_client, method, path, extra, *_args, **_kwargs):
            seen.append(path)
            return response

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_request", side_effect=fake_request
        ), patch.object(media, "_persistent_mobile_device", return_value=("1" * 16, "2" * 16)), patch.object(
            media, "_mobile_device_profile", return_value={"own_uid": "551"}
        ):
            items, source = media.fetch_stories_via_mobile_story_feed(
                object(), "sec", user_id="1234567890", cookie_header="sid=1"
            )

        self.assertEqual([], items)
        self.assertIn(media.STORY_PROFILE_LIST_PATH, source)
        self.assertIn("empty pack", source)
        self.assertEqual([media.STORY_PROFILE_LIST_PATH, media.STORY_FEED_PATH], seen)

    def test_profile_list_and_story_feed_results_are_merged(self):
        # 主页挂 3 条日常时：profile/list 回 TTL 内的那条，story/feed 回更早的
        # 历史日常。以前命中 profile/list 就 return，导致只拿到 1 条。
        active = {"aweme_id": "active-story", "is_story": 1, "author": {"sec_uid": "sec"}}
        history = {"aweme_id": "history-story", "is_story": 1, "author": {"sec_uid": "sec"}}
        payloads = {
            media.STORY_PROFILE_LIST_PATH: {
                "status_code": 0,
                "active_data": {"data": [active], "has_more": False},
                "month_list": [],
            },
            media.STORY_FEED_PATH: {"status_code": 0, "data": [history], "has_more": False},
        }
        seen = []

        def fake_request(_client, method, path, extra, *_args, **_kwargs):
            seen.append(path)
            response = MagicMock()
            response.json.return_value = payloads[path]
            return response

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_request", side_effect=fake_request
        ), patch.object(media, "_persistent_mobile_device", return_value=("1" * 16, "2" * 16)), patch.object(
            media, "_mobile_device_profile", return_value={"own_uid": "551"}
        ):
            items, source = media.fetch_stories_via_mobile_story_feed(
                object(), "sec", user_id="1234567890", cookie_header="sid=1"
            )

        self.assertEqual(["active-story", "history-story"], [i["aweme_id"] for i in items])
        self.assertEqual([media.STORY_PROFILE_LIST_PATH, media.STORY_FEED_PATH], seen)

    def test_story_feed_uses_to_uid_not_tray_params(self):
        # 历史日常只认作者维度（to_uid）；关注页托盘参数一条都拿不到，
        # 这里锁死请求形态，避免以后被改回 cursor 形态。
        captured = {}

        def fake_request(_client, method, path, extra, *_args, **_kwargs):
            captured.setdefault(path, extra)
            response = MagicMock()
            response.json.return_value = {"status_code": 0}
            return response

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_request", side_effect=fake_request
        ), patch.object(media, "_persistent_mobile_device", return_value=("1" * 16, "2" * 16)), patch.object(
            media, "_mobile_device_profile", return_value={"own_uid": "551"}
        ):
            media.fetch_stories_via_mobile_story_feed(
                object(), "sec", user_id="1234567890", cookie_header="sid=1"
            )

        feed_extra = captured[media.STORY_FEED_PATH]
        self.assertEqual("1234567890", feed_extra.get("to_uid"))
        self.assertNotIn("cursor", feed_extra)
        self.assertEqual("0", feed_extra.get("story_ttl"))

    def test_mobile_story_feed_is_used_before_web_fallbacks(self):
        target = "target-sec-uid"
        feed_items = [
            {"aweme_id": "ring-story", "is_story": 1, "author": {"sec_uid": target}},
        ]

        with patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")), patch.object(
            media,
            "fetch_stories_via_mobile_story_feed",
            return_value=(feed_items, "https://aweme.snssdk.com/aweme/v1/story/feed/"),
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(media, "request_life_feed", return_value=(None, "life skipped")), patch.object(
            media, "request_json", return_value={"status_code": 0, "data": []}
        ):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertTrue(supported)
        self.assertIn("story/feed", source)
        self.assertEqual(["ring-story"], [item["aweme_id"] for item in items])

    def test_post_feed_candidates_do_not_mask_real_story_apis(self):
        """作品流里的"疑似日常"必须让位给真正的 story 接口。

        回归：以前第 1 级命中就 return，真正的 24h/日常 接口整条被跳过，
        表现为主页挂着日常却一条都抓不到。
        """
        target = "target-sec-uid"
        post_items = [
            {"aweme_id": "false-story", "is_story": 1, "author": {"sec_uid": target}},
        ]
        real_items = [
            {"aweme_id": "real-story", "is_25_story": 1, "author": {"sec_uid": target}},
        ]
        with patch.object(
            media, "fetch_stories_via_mobile_post_api",
            return_value=(post_items, "mobile post feed"),
        ) as post_mock, patch.object(
            media, "fetch_stories_via_mobile_story_feed",
            return_value=(real_items, f"https://aweme.snssdk.com{media.STORY_PROFILE_LIST_PATH}"),
        ) as feed_mock, patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(
            media, "request_life_feed", return_value=(None, "life skipped")
        ), patch.object(media, "request_json", return_value={"status_code": 0, "data": []}):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        # 真接口被调用了，并且返回的是它，而不是第 1 级的伪日常。
        self.assertTrue(feed_mock.called)
        post_mock.assert_not_called()
        self.assertEqual(["real-story"], [item["aweme_id"] for item in items])
        self.assertIn("story/profile/list", source)
        self.assertNotIn("fallback", source)

    def test_post_feed_candidates_are_used_only_as_last_resort(self):
        """所有真实 story 接口都空时，才回退到作品流的疑似日常。"""
        target = "target-sec-uid"
        post_items = [
            {"aweme_id": "fallback-story", "is_story": 1, "author": {"sec_uid": target}},
        ]
        with patch.object(
            media, "fetch_stories_via_mobile_post_api",
            return_value=(post_items, "mobile post feed"),
        ), patch.object(
            media, "fetch_stories_via_mobile_story_feed", return_value=(None, "no items")
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no items")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ), patch.object(
            media, "request_life_feed", return_value=(None, "life skipped")
        ), patch.object(
            media, "request_json", return_value={"status_code": 0, "data": []}
        ), patch.object(media, "profile_has_active_story", return_value=False):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertEqual(["fallback-story"], [item["aweme_id"] for item in items])
        self.assertIn("fallback", source)

    def test_mobile_story_feed_tries_story25_profile_list(self):
        payload = {
            "status_code": 0,
            "active_data": {
                "data": [
                    {"aweme_id": "story25-ring", "is_25_story": 1, "is_story": 1},
                ]
            },
        }
        response = MagicMock()
        response.json.return_value = payload
        seen = []

        def fake_request(_client, method, path, extra, *_args, **_kwargs):
            seen.append((path, dict(extra or {})))
            if path == media.STORY_PROFILE_LIST_PATH:
                return response
            empty = MagicMock()
            empty.json.return_value = {"status_code": 0, "data": None}
            return empty

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_request", side_effect=fake_request
        ), patch.object(media, "_persistent_mobile_device", return_value=("1" * 16, "2" * 16)), patch.object(
            media, "_mobile_device_profile", return_value={"own_uid": "551"}
        ):
            items, source = media.fetch_stories_via_mobile_story_feed(
                object(), "sec", user_id="4081005313657150", cookie_header="sid=1"
            )

        self.assertEqual(["story25-ring"], [item["aweme_id"] for item in items])
        self.assertIn(media.STORY_PROFILE_LIST_PATH, source)
        self.assertTrue(seen)
        self.assertEqual(media.STORY_PROFILE_LIST_PATH, seen[0][0])
        self.assertEqual("4081005313657150", seen[0][1].get("to_uid"))
        self.assertEqual("7", seen[0][1].get("story_ttl"))

    def test_story_feed_payload_unwraps_active_data(self):
        payload = {
            "status_code": 0,
            "active_data": {
                "data": [{"aweme_id": "from-active", "is_25_story": 1, "is_story": 1}],
            },
        }
        items = media._story_items_from_feed_payload(payload)
        self.assertEqual(["from-active"], [item["aweme_id"] for item in items])

    def test_story_feed_payload_merges_data_and_active_data_buckets(self):
        """The two response shapes (data / active_data) are both read."""
        payload = {
            "status_code": 0,
            "data": [{"aweme_id": "feed-story", "is_25_story": 1}],
            "active_data": {"data": [{"aweme_id": "profile-story", "is_25_story": 1}]},
        }

        items = media._story_items_from_feed_payload(payload)

        self.assertEqual(
            {"feed-story", "profile-story"},
            {item["aweme_id"] for item in items},
        )

    def test_empty_profile_list_still_returns_story_feed_history(self):
        # profile/list 只覆盖 TTL 内的日常，它返回空并不代表这个主页没有日常；
        # story/feed 里的历史日常仍然必须被返回，而不是直接报"没有日常"。
        payloads = {
            media.STORY_PROFILE_LIST_PATH: {"status_code": 0, "active_data": {"data": []}},
            media.STORY_FEED_PATH: {
                "status_code": 0,
                "data": [{"aweme_id": "old-story", "is_story": 1, "author": {"sec_uid": "sec"}}],
            },
        }

        def fake_request(_client, method, path, extra, *_args, **_kwargs):
            response = MagicMock()
            response.json.return_value = payloads[path]
            return response

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_request", side_effect=fake_request
        ), patch.object(media, "_persistent_mobile_device", return_value=("1" * 16, "2" * 16)), patch.object(
            media, "_mobile_device_profile", return_value={"own_uid": "551"}
        ):
            items, source = media.fetch_stories_via_mobile_story_feed(
                object(), "sec", user_id="1234567890", cookie_header="sid=1"
            )

        self.assertEqual(["old-story"], [item["aweme_id"] for item in items])
        self.assertIn(media.STORY_FEED_PATH, source)

    def test_time_limited_type68_post_is_still_a_story(self):
        target = "target-sec-uid"
        story_note = {
            "aweme_id": "daily-note",
            "aweme_type": 68,
            "is_25_story": 1,
            "author": {"sec_uid": target},
        }

        with patch.object(
            media,
            "fetch_stories_via_mobile_post_api",
            return_value=([story_note], "https://aweme.snssdk.com/aweme/v1/aweme/post/ (mobile, 1 stories)"),
        ), patch.object(
            media, "fetch_stories_via_mobile_story_feed", return_value=(None, "no feed")
        ), patch.object(
            media, "fetch_stories_via_mobile_life_feed", return_value=(None, "no life")
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertTrue(supported)
        self.assertEqual(["daily-note"], [item["aweme_id"] for item in items])

    def test_mobile_post_api_ignores_unmarked_image_notes(self):
        payload = {
            "status_code": 0,
            "has_more": 0,
            "max_cursor": 0,
            "aweme_list": [
                {"aweme_id": "image-note", "aweme_type": 68, "is_story": 0, "is_25_story": 0},
                {"aweme_id": "daily", "aweme_type": 68, "is_25_story": 1},
            ],
        }
        response = MagicMock()
        response.json.return_value = payload

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_get", return_value=response
        ):
            items, source = media.fetch_stories_via_mobile_post_api(object(), "sec", cookie_header="sid=1")

        self.assertEqual(["daily"], [item["aweme_id"] for item in items])
        self.assertIn("1 stories", source)

    def test_mobile_life_feed_is_used_when_story_feed_is_empty(self):
        target = "target-sec-uid"
        life_items = [
            {"aweme_id": "life-ring", "is_story": 1, "author": {"sec_uid": target}},
        ]

        with patch.object(media, "fetch_stories_via_mobile_post_api", return_value=(None, "no post stories")), patch.object(
            media, "fetch_stories_via_mobile_story_feed", return_value=(None, "empty pack")
        ), patch.object(
            media,
            "fetch_stories_via_mobile_life_feed",
            return_value=(life_items, "https://aweme.snssdk.com/aweme/v1/life/feed/"),
        ), patch.object(
            media, "fetch_stories_via_browser", return_value=(None, "no browser")
        ):
            items, source, supported = media.fetch_stories(object(), {"cookies": "x"}, target)

        self.assertTrue(supported)
        self.assertIn("life/feed", source)
        self.assertEqual(["life-ring"], [item["aweme_id"] for item in items])

    def test_story_feed_payload_unwraps_user_packs(self):
        payload = {
            "status_code": 0,
            "data": [
                {
                    "user": {"sec_uid": "abc"},
                    "story_list": [{"aweme_id": "from-pack", "is_story": 1}],
                }
            ],
        }
        items = media._story_items_from_feed_payload(payload)
        self.assertEqual(["from-pack"], [item["aweme_id"] for item in items])

    def test_download_uses_local_emulator_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "cached.mp4"
            source.write_bytes(b"\x00\x00\x00 ftypisomlocal" + b"\0" * 4096)
            aweme = {
                "aweme_id": "local-story",
                "desc": "日常",
                "create_time": 1786893660,
                "is_25_story": 1,
                "_local_media_path": str(source),
            }
            profile = {"id": "t", "name": "T", "output_dir": temporary_directory}
            result = media.download_aweme_items(
                object(), profile, [aweme], temporary_directory, {"downloaded_story_ids": []}, "story"
            )
            self.assertEqual(1, result.downloaded)
            saved = Path(result.files[0])
            self.assertTrue(saved.is_file())
            self.assertEqual(source.read_bytes(), saved.read_bytes())

    def test_recorded_story_without_local_file_is_downloaded_again(self):
        """A recorded id must not block the download once its file is gone.

        The state file is merged forward on every save, so cleaning up the
        download folder by hand leaves ids behind that point at nothing.
        """
        aweme = {
            "aweme_id": "residue-story",
            "desc": "日常",
            "create_time": 1786893660,
            "video": {"play_addr": {"url_list": ["https://example.test/story.mp4"]}},
        }
        state = {"downloaded_story_ids": ["residue-story"]}

        def fake_download(_client, url, output_path, progress_callback=None,
                          progress_details=None, **_kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"\x00\x00\x00 ftypisomdownloaded")

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, state, "story"
            )

        self.assertEqual(0, result.skipped)
        self.assertEqual(1, result.downloaded)
        self.assertEqual(0, result.failed)
        self.assertEqual(["residue-story"], state["downloaded_story_ids"])

    def test_recorded_story_with_present_file_is_still_skipped(self):
        aweme = {
            "aweme_id": "present-story",
            "desc": "日常",
            "create_time": 1786893660,
            "video": {"play_addr": {"url_list": ["https://example.test/story.mp4"]}},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "stories"
            target.mkdir(parents=True, exist_ok=True)
            (target / media.aweme_filename(aweme)).write_bytes(b"\x00\x00\x00 ftypisom" + b"\0" * 4096)
            state = {"downloaded_story_ids": ["present-story"]}
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, state, "story"
            )

        self.assertEqual(1, result.skipped)
        self.assertEqual(0, result.downloaded)

    def test_recorded_image_story_without_local_images_is_downloaded_again(self):
        """Image items store <stem>_01.jpg, never the .mp4 output_path."""
        aweme = {
            "aweme_id": "residue-image-story",
            "desc": "图文日常",
            "create_time": 1786893660,
            "images": [{"url_list": ["https://example.test/one.jpg"]}],
        }
        state = {"downloaded_story_ids": ["residue-image-story"]}
        saved_paths = []

        def fake_download(_client, url, output_path, progress_callback=None,
                          progress_details=None, **_kwargs):
            saved_paths.append(str(output_path))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"jpegdata")

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, state, "story"
            )

        self.assertEqual(0, result.skipped)
        self.assertEqual(1, result.downloaded)
        self.assertTrue(saved_paths and saved_paths[0].endswith("_01.jpg"))

    def test_parse_share_command_blk(self):
        blob = b"keva-blk\x00https://v.douyin.com/EXY01FyD8dU/\x00junk"
        self.assertEqual("https://v.douyin.com/EXY01FyD8dU/", media._parse_share_command_blk(blob))

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

    def test_metadata_json_is_opt_in(self):
        """OPT-META: the <file>.json sidecar is written only when asked for."""
        aweme = {
            "aweme_id": "meta-note",
            "desc": "Metadata sidecar",
            "images": [{"url_list": ["https://example.test/photo.jpg"]}],
        }

        def fake_download(_client, url, output_path, progress_callback=None, progress_details=None):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"image")

        def run(**kwargs):
            """Return the parsed sidecar payloads (temp dir is gone afterwards)."""
            with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
                media, "download_bytes", side_effect=fake_download
            ):
                media.download_aweme_items(
                    object(), {}, [aweme], temporary_directory, {}, "video", **kwargs
                )
                return [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in Path(temporary_directory).rglob("*.json")
                ]

        # Default: media only, no metadata sidecar.
        self.assertEqual([], run())
        # Opt-in: exactly one sidecar, holding the raw payload.
        self.assertEqual([aweme], run(save_metadata_json=True))

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

    def test_live_photo_image_entry_exposes_animated_payload(self):
        """动图 (Live Photo) works carry their mp4 in images[i].video."""
        aweme = {
            "aweme_id": "live-photo-note",
            "aweme_type": 68,
            "is_live_photo": 1,
            "images": [
                {
                    "clip_type": 5,
                    "live_photo_type": 1,
                    "url_list": ["https://example.test/cover.heic"],
                    "video": {
                        "bit_rate": [
                            {"play_addr": {"url_list": ["https://example.test/live.mp4"]}}
                        ]
                    },
                }
            ],
        }

        entries = media.collect_image_entries(aweme)

        self.assertEqual(1, len(entries))
        self.assertEqual("https://example.test/cover.heic", entries[0]["url"])
        self.assertEqual(["https://example.test/live.mp4"], entries[0]["video_urls"])
        self.assertTrue(entries[0]["live_photo"])
        # collect_image_urls stays still-image-only for existing callers.
        self.assertEqual(["https://example.test/cover.heic"], media.collect_image_urls(aweme))

    def test_plain_image_entry_has_no_animated_payload(self):
        aweme = {"images": [{"url_list": ["https://example.test/one.jpg"]}]}

        entries = media.collect_image_entries(aweme)

        self.assertEqual(1, len(entries))
        self.assertEqual("https://example.test/one.jpg", entries[0]["url"])
        self.assertEqual([], entries[0]["video_urls"])
        self.assertFalse(entries[0]["live_photo"])

    def test_live_photo_work_downloads_still_and_animated_mp4(self):
        aweme = {
            "aweme_id": "live-photo-download",
            "aweme_type": 68,
            "is_live_photo": 1,
            "images": [
                {
                    "url_list": ["https://example.test/cover.jpg"],
                    "video": {
                        "play_addr": {"url_list": ["https://example.test/live.mp4"]}
                    },
                }
            ],
        }
        saved_urls = []

        def fake_download(_client, url, output_path, progress_callback=None,
                          progress_details=None, **_kwargs):
            saved_urls.append(url)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(url.encode("utf-8"))

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, {}, "video"
            )
            images_dir = Path(temporary_directory) / "images"
            stills = sorted(images_dir.glob("*.jpg"))
            animations = sorted(images_dir.glob("*.mp4"))

        self.assertEqual(1, result.downloaded)
        self.assertEqual(1, len(stills))
        self.assertEqual(1, len(animations))
        self.assertTrue(stills[0].name.endswith("_01.jpg"))
        self.assertTrue(animations[0].name.endswith("_01_live.mp4"))
        self.assertIn("https://example.test/live.mp4", saved_urls)

    def test_recorded_live_photo_missing_mp4_is_downloaded_again(self):
        """A still-only run from an older version must fetch the 动图 later."""
        aweme = {
            "aweme_id": "legacy-live-photo",
            "desc": "图文日常",
            "create_time": 1786893660,
            "images": [
                {
                    "url_list": ["https://example.test/cover.jpg"],
                    "video": {
                        "play_addr": {"url_list": ["https://example.test/live.mp4"]}
                    },
                }
            ],
        }
        state = {"downloaded_story_ids": ["legacy-live-photo"]}

        def fake_download(_client, url, output_path, progress_callback=None,
                          progress_details=None, **_kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(url.encode("utf-8"))

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            media, "download_bytes", side_effect=fake_download
        ):
            target = Path(temporary_directory) / "stories"
            target.mkdir(parents=True, exist_ok=True)
            stem = media.aweme_filename(aweme, suffix="")
            (target / f"{stem}_01.jpg").write_bytes(b"jpegfromolderbuild")
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, state, "story"
            )
            animations = sorted(target.glob("*.mp4"))

        self.assertEqual(0, result.skipped)
        self.assertEqual(1, result.downloaded)
        self.assertEqual(1, len(animations))
        self.assertTrue(animations[0].name.endswith("_01_live.mp4"))

    def test_recorded_live_photo_with_all_payloads_is_still_skipped(self):
        aweme = {
            "aweme_id": "complete-live-photo",
            "desc": "图文日常",
            "create_time": 1786893660,
            "images": [
                {
                    "url_list": ["https://example.test/cover.jpg"],
                    "video": {
                        "play_addr": {"url_list": ["https://example.test/live.mp4"]}
                    },
                }
            ],
        }
        state = {"downloaded_story_ids": ["complete-live-photo"]}

        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "stories"
            target.mkdir(parents=True, exist_ok=True)
            stem = media.aweme_filename(aweme, suffix="")
            (target / f"{stem}_01.jpg").write_bytes(b"\xff\xd8\xff" + b"\0" * 4096)
            (target / f"{stem}_01_live.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"\0" * 4096)
            result = media.download_aweme_items(
                object(), {}, [aweme], temporary_directory, state, "story"
            )

        self.assertEqual(1, result.skipped)
        self.assertEqual(0, result.downloaded)

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
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
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

    def test_import_chrome_session_saves_app_capable_login(self):
        cookies = [
            {"name": "sessionid", "value": "sid", "domain": ".douyin.com", "path": "/", "expires": 0},
            {"name": "uid_tt", "value": "uid", "domain": "www.douyin.com", "path": "/", "expires": 0},
        ]
        list_response = MagicMock()
        list_response.json.return_value = [
            {
                "type": "page",
                "url": "https://www.douyin.com/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9344/devtools/page/1",
            }
        ]
        list_response.raise_for_status.return_value = None
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = list_response

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_file = root / "douyin_session.json"
            mobile_file = root / "mobile_session.json"
            device_file = root / "mobile_device.json"
            with patch.object(media.httpx, "Client", return_value=client), patch.object(
                media, "chrome_cdp_command", return_value={"cookies": cookies}
            ), patch.object(media, "close_cdp_browser") as close_browser, patch.object(
                media, "SESSION_FILE", session_file
            ), patch.object(
                media, "MOBILE_SESSION_FILE", mobile_file
            ), patch.object(media, "MOBILE_DEVICE_FILE", device_file), patch.object(
                media, "dpapi_protect", side_effect=lambda data: b"enc-" + data
            ), patch.object(
                media, "dpapi_unprotect", side_effect=lambda data: data[4:]
            ):
                result = media.import_chrome_session("http://127.0.0.1:9344")

            close_browser.assert_called_once_with("http://127.0.0.1:9344")
            self.assertTrue(result["app_capable"])
            self.assertTrue(session_file.is_file())
            self.assertTrue(mobile_file.is_file())
            device = media.load_json(device_file, {})
            self.assertTrue(str(device.get("device_id") or "").isdigit())
            self.assertTrue(str(device.get("install_id") or "").isdigit())
            self.assertTrue(str(device.get("cdid") or ""))
            with patch.object(media, "SESSION_FILE", session_file), patch.object(
                media, "MOBILE_SESSION_FILE", mobile_file
            ), patch.object(media, "MOBILE_DEVICE_FILE", device_file), patch.object(
                media, "dpapi_protect", side_effect=lambda data: b"enc-" + data
            ), patch.object(
                media, "dpapi_unprotect", side_effect=lambda data: data[4:]
            ):
                self.assertIn("sessionid=sid", media.load_session_cookie_header())
                self.assertIn("sessionid=sid", media.load_mobile_session_cookie_header())
                info = media.saved_session_info()
            self.assertTrue(info["logged_in"])
            self.assertTrue(info["app_capable"])

    def test_import_chrome_session_rejects_remote_cdp(self):
        with self.assertRaises(RuntimeError):
            media.import_chrome_session("http://192.168.1.5:9222")

    def test_download_bytes_rejects_untrusted_host(self):
        with self.assertRaises(ValueError):
            media.download_bytes(object(), "https://evil.example/video.mp4", Path("out.mp4"))

    def test_fetch_posts_via_mobile_api_reuses_bound_device(self):
        payload = {
            "status_code": 0,
            "has_more": 0,
            "max_cursor": 0,
            "aweme_list": [{"aweme_id": "one", "desc": "post"}],
        }
        response = MagicMock()
        response.json.return_value = payload
        seen = []

        def fake_get(_client, _path, extra, cookie, device_id, install_id, **_kwargs):
            seen.append((cookie, device_id, install_id, dict(extra or {})))
            return response

        with patch.object(media, "_check_mobile_signer", return_value=True), patch.object(
            media, "_mobile_signed_get", side_effect=fake_get
        ), patch.object(
            media, "_persistent_mobile_device", return_value=("1" * 16, "2" * 16)
        ), patch.object(media, "_mobile_cookie_header", return_value="sessionid=app"):
            items = media.fetch_posts_via_mobile_api(object(), "sec", limit=1)

        self.assertEqual(["one"], [item["aweme_id"] for item in items])
        self.assertEqual("sessionid=app", seen[0][0])
        self.assertEqual("1" * 16, seen[0][1])
        self.assertEqual("2" * 16, seen[0][2])

    def test_promote_web_session_copies_cookies_and_binds_device(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_file = root / "douyin_session.json"
            mobile_file = root / "mobile_session.json"
            device_file = root / "mobile_device.json"
            browser_dir = root / "edge-profile"
            (browser_dir / "Default").mkdir(parents=True)
            (browser_dir / "Default" / "Cookies").write_text("cookie-db", encoding="utf-8")
            with patch.object(media, "SESSION_FILE", session_file), patch.object(
                media, "MOBILE_SESSION_FILE", mobile_file
            ), patch.object(media, "MOBILE_DEVICE_FILE", device_file), patch.object(
                media, "FETCH_BROWSER_PROFILE_DIR", browser_dir
            ), patch.object(media, "dpapi_protect", side_effect=lambda data: b"enc-" + data), patch.object(
                media, "dpapi_unprotect", side_effect=lambda data: data[4:]
            ):
                media.save_session_cookie_header("sessionid=web; uid_tt=u", source="test")
                header = media.promote_web_session_to_app()
                self.assertIn("sessionid=web", header)
                self.assertIn("sessionid=web", media.load_mobile_session_cookie_header())
                self.assertTrue(device_file.exists())
                profile = media.apply_saved_session({})
                self.assertIn("sessionid=web", profile["cookies"])
                media.clear_saved_session()
                self.assertFalse(session_file.exists())
                self.assertFalse(mobile_file.exists())
                self.assertFalse(device_file.exists())
                self.assertFalse((browser_dir / "Default" / "Cookies").exists())
                self.assertEqual("", media._mobile_cookie_header())

    def test_aweme_filename_sanitizes_path_traversal_ids(self):
        name = media.aweme_filename(
            {
                "aweme_id": r"..\..\evil/id",
                "desc": "clip",
                "create_time": 1_700_000_000,
            }
        )
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertTrue(name.endswith("_clip.mp4") or "_clip.mp4" in name)

    def test_aweme_filename_budgets_caption_against_deep_base_dir(self):
        # Regression: a long CJK caption under a ~135-char portable install root
        # used to push the real path past Windows' 260-char MAX_PATH, so open()
        # raised FileNotFoundError and the work was reported as a failed
        # download even though the CDN served it fine.
        sec_uid = "MS4wLjABAAAA" + "0" * 43   # 55 chars, same shape as a real one
        deep = Path(
            "C:/Users/someone/Downloads/DouyinLiveRecorder-v1.2.6-win64"
            f"/douyindownload/抖音 {sec_uid}/videos"
        )
        aweme = {
            "aweme_id": "7000000000000000000",
            "desc": "示例标题示例标题示例标题！✴️#examplehashtag  #示例话题标签",
            "create_time": 1_709_900_648,
        }
        for suffix in ("", ".mp4"):
            name = media.aweme_filename(aweme, suffix=suffix, base_dir=deep)
            # Worst case the caller appends "_01.jpg" and download_bytes then
            # appends ".part.<pid>.<8-hex-uuid>".
            worst = (
                len(str(deep)) + 1 + len(name)
                + len("_01.jpg") + len(".part.99999.") + 8
            )
            self.assertLess(worst, 260, f"path too long for suffix={suffix!r}: {worst}")
            # The id must survive so state tracking keeps working.
            self.assertIn("7000000000000000000", name)

    def test_aweme_filename_keeps_short_captions_intact(self):
        aweme = {"aweme_id": "123", "desc": "短的标题", "create_time": 1_700_000_000}
        name = media.aweme_filename(aweme, base_dir=Path("C:/tmp/videos"))
        self.assertIn("短的标题", name)

    def test_media_item_path_groups_payloads_under_aweme_id(self):
        target = Path("C:/tmp/videos")
        aweme = {
            "aweme_id": "7000000000000000000",
            "desc": "示例标题",
            "create_time": 1_709_900_648,
        }
        flat = media.media_item_path(target, aweme, subdir_by_aweme_id=False)
        grouped = media.media_item_path(target, aweme, subdir_by_aweme_id=True, keep_full_title=True)
        self.assertEqual(flat.parent, target)
        self.assertIn("7000000000000000000", flat.name)
        self.assertEqual(grouped.parent, target / "7000000000000000000")
        # the id names the directory, so it must not be repeated in the file name
        self.assertNotIn("7000000000000000000", grouped.name)
        self.assertIn("示例标题", grouped.name)

    def test_media_item_path_keeps_long_caption_when_requested(self):
        target = Path("C:/tmp/videos")
        caption = "示例标题示例标题示例标题！✴️#examplehashtag #示例话题标签"
        aweme = {
            "aweme_id": "7000000000000000000",
            "desc": caption,
            "create_time": 1_709_900_648,
        }
        full = media.media_item_path(target, aweme, keep_full_title=True)
        self.assertIn(caption, full.name)
        # The truncating layout still shortens it against a deep directory.
        # (At extreme depths even the floor of 16 chars can exceed 260; that case
        # is what _extended_path() exists to cover.)
        deep = Path("C:/" + "d" * 150)
        short = media.media_item_path(deep, aweme, keep_full_title=False)
        self.assertNotEqual(short.name, full.name)
        self.assertLess(len(short.name), len(full.name))
        self.assertLess(len(str(short)) + media._PATH_TAIL_RESERVE, media._MAX_PATH_LEGACY)

    def test_extended_path_only_rewrites_over_long_windows_paths(self):
        short = "C:/tmp/videos/a.mp4"
        self.assertEqual(media._extended_path(short), short)
        long_path = "C:/tmp/" + "d" * 300 + "/a.mp4"
        extended = media._extended_path(long_path)
        if os.name == "nt":
            self.assertTrue(extended.startswith("\\\\?\\"))
            self.assertTrue(extended.endswith("a.mp4"))
            # already-prefixed paths must be left alone
            self.assertEqual(media._extended_path(extended), extended)
        else:
            self.assertEqual(extended, long_path)

    def test_normalize_pasted_link_extracts_url_from_share_blob(self):
        blob = (
            "1- 长按复制此条消息，打开抖音搜索，查看TA的更多作品。 "
            "https://v.douyin.com/AbCdEfGhIjK/ 2@2.com :4pm"
        )
        self.assertEqual(
            media.normalize_pasted_link(blob), "https://v.douyin.com/AbCdEfGhIjK/"
        )
        # The whole sentence must never survive into the request layer.
        self.assertNotIn("长按复制", media.normalize_pasted_link(blob))

    def test_normalize_pasted_link_tolerates_missing_scheme(self):
        self.assertEqual(
            media.normalize_pasted_link("v.douyin.com/abc/ 复制打开抖音"),
            "https://v.douyin.com/abc/",
        )
        self.assertEqual(media.normalize_pasted_link(""), "")
        self.assertEqual(media.normalize_pasted_link("没有链接的文字"), "")

    def test_canonical_douyin_profile_url_handles_share_landing_page(self):
        sec_uid = "MS4wLjABAAAA" + "0" * 43
        canonical = f"https://www.douyin.com/user/{sec_uid}"
        # The v.douyin.com redirect lands on the share surface, which the app
        # previously failed to recognize as a profile at all.
        self.assertEqual(
            media.extract_sec_uid_from_url(
                f"https://www.iesdouyin.com/share/user/{sec_uid}?u_code=abc&did=xyz"
            ),
            sec_uid,
        )
        self.assertEqual(
            media.canonical_douyin_profile_url(
                f"https://www.iesdouyin.com/share/user/{sec_uid}?u_code=abc"
            ),
            canonical,
        )
        self.assertEqual(
            media.canonical_douyin_profile_url(f"{canonical}?from_tab_name=main"),
            canonical,
        )
        self.assertEqual(media.canonical_douyin_profile_url("https://v.douyin.com/abc/"), "")

    def test_expand_douyin_short_link_short_circuits_concrete_urls(self):
        # Concrete URLs must return without touching the network.
        canonical = "https://www.douyin.com/user/MS4wLjABAAAAexample"
        self.assertEqual(media.expand_douyin_short_link(canonical), canonical)
        work = "https://www.douyin.com/video/7000000000000000001"
        self.assertEqual(media.expand_douyin_short_link(work), work)
        self.assertEqual(media.expand_douyin_short_link(""), "")

    def test_mobile_base_params_reuse_stable_cdid(self):
        with patch.object(
            media,
            "_mobile_device_profile",
            return_value={
                "os_api": "35",
                "os_version": "15",
                "device_type": "SM-A5560",
                "device_brand": "samsung",
                "channel": "channel_aweme",
                "version_code": "380700",
                "version_name": "38.7.0",
                "update_version_code": "380700",
                "cdid": "11111111-2222-3333-4444-555555555555",
            },
        ):
            first = media._mobile_base_params("1" * 16, "2" * 16)
            second = media._mobile_base_params("1" * 16, "2" * 16)
        self.assertEqual("11111111-2222-3333-4444-555555555555", first["cdid"])
        self.assertEqual(first["cdid"], second["cdid"])
        self.assertEqual("channel_aweme", first["channel"])
        self.assertEqual("380700", first["update_version_code"])


if __name__ == "__main__":
    unittest.main()
