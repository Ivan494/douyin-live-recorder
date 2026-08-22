import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from security_utils import (
    follow_safe_redirects,
    is_loopback_cdp_url,
    is_safe_http_url,
    is_safe_media_download_url,
    is_safe_recording_url,
    is_safe_share_link_url,
    resolve_trusted_executable,
)


class SecurityUtilsTest(unittest.TestCase):
    def test_loopback_cdp_urls(self):
        self.assertTrue(is_loopback_cdp_url("http://127.0.0.1:9344"))
        self.assertTrue(is_loopback_cdp_url("http://localhost:9222"))
        self.assertFalse(is_loopback_cdp_url("http://192.168.1.1:9222"))
        self.assertFalse(is_loopback_cdp_url("ws://127.0.0.1:9222/devtools"))

    def test_recording_url_rejects_private_and_file_schemes(self):
        self.assertTrue(is_safe_recording_url("https://cdn.example/live.flv"))
        self.assertFalse(is_safe_recording_url("file:///etc/passwd"))
        self.assertFalse(is_safe_recording_url("http://127.0.0.1/live.flv"))
        self.assertFalse(is_safe_recording_url("http://192.168.0.5/live.flv"))

    def test_share_link_allowlist(self):
        self.assertTrue(is_safe_share_link_url("https://v.douyin.com/abc/"))
        self.assertTrue(is_safe_share_link_url("https://www.douyin.com/video/123"))
        self.assertFalse(is_safe_share_link_url("https://evil.example/redirect"))

    def test_media_download_allowlist(self):
        self.assertTrue(is_safe_media_download_url("https://aweme.snssdk.com/aweme/v1/play/"))
        self.assertTrue(is_safe_media_download_url("https://p3.douyinpic.com/obj/example.jpg"))
        self.assertFalse(is_safe_media_download_url("https://example.com/video.mp4"))

    def test_follow_safe_redirects_rejects_bad_hop(self):
        first = MagicMock()
        first.is_redirect = True
        first.headers = {"location": "https://evil.example/final"}
        first.url.join.return_value = "https://evil.example/final"
        client = MagicMock()
        client.get.return_value = first

        with self.assertRaises(ValueError):
            follow_safe_redirects(
                client,
                "https://v.douyin.com/abc/",
                url_validator=is_safe_share_link_url,
            )

    def test_resolve_trusted_executable_requires_allowed_basename(self):
        with self.assertRaises(ValueError):
            resolve_trusted_executable("cmd.exe", trusted_roots=(Path("/workspace"),))

    def test_resolve_trusted_executable_accepts_file_under_trusted_root(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tool = root / "ffmpeg.exe"
            tool.write_text("stub", encoding="utf-8")
            resolved = resolve_trusted_executable(
                str(tool),
                allowed_basenames={"ffmpeg.exe"},
                trusted_roots=(root,),
            )
            self.assertEqual(str(tool.resolve()), resolved)

    def test_is_safe_http_url_alias(self):
        self.assertEqual(
            is_safe_http_url("https://cdn.example/live.flv"),
            is_safe_recording_url("https://cdn.example/live.flv"),
        )


if __name__ == "__main__":
    unittest.main()
