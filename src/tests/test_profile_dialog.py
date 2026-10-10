import gc
import os
import tempfile
import threading
import unittest
from pathlib import Path
from tkinter import Tk
from unittest.mock import patch

import douyin_recorder_app as app
from douyin_recorder_app import ProfileDialog


class FakeStore:
    settings = {
        "quality": "OD",
        "new_profile_poll_interval_seconds": 60,
        "media_poll_interval_seconds": 300,
    }


@unittest.skipUnless(os.name == "nt", "Tk profile dialog smoke test is Windows-only")
class ProfileDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        del cls.root
        gc.collect()

    def tearDown(self):
        # Release widget/Variable cycles on the Tk thread before later tests
        # create workers that could otherwise trigger their garbage collection.
        gc.collect()

    def test_save_short_share_link_resolves_off_the_tk_thread(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        dialog.url_var.set("复制打开抖音 https://v.douyin.com/author/ 查看更多作品")
        dialog.name_var.set("Author")
        started = threading.Event()
        release = threading.Event()
        ui_thread = threading.get_ident()
        canonical = "https://www.douyin.com/user/test-author"

        def expand(url, **kwargs):
            self.assertNotEqual(ui_thread, threading.get_ident(), "Save performed HTTP on the Tk thread")
            started.set()
            self.assertTrue(release.wait(2))
            return canonical

        try:
            with patch.object(app, "expand_douyin_short_link", side_effect=expand), patch.object(
                app.ProfileDialog, "resolve_room", side_effect=AssertionError("Profile must not query a live room")
            ):
                dialog.save()
                self.assertIsNone(dialog.result)
                self.assertTrue(started.wait(1))
                # Tk can still process events while the simulated request waits.
                self.root.update()
                release.set()
                result = dialog.resolve_result_queue.get(timeout=2)
                dialog.resolve_result_queue.put(result)
                dialog.process_resolve_results()
                self.assertEqual(canonical, dialog.result["url"])
                self.assertEqual(canonical, dialog.result["original_profile_url"])
                self.assertEqual("Author", dialog.result["name"])
        finally:
            release.set()
            dialog.destroy()

    def test_save_direct_share_profile_needs_no_http_or_name(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        dialog.url_var.set("主页 https://www.iesdouyin.com/share/user/author?from=share")
        try:
            with patch.object(app, "expand_douyin_short_link", side_effect=AssertionError("Save must not perform HTTP")):
                dialog.save()
            self.assertEqual("https://www.douyin.com/user/author", dialog.result["url"])
            self.assertEqual(dialog.result["url"], dialog.result["original_profile_url"])
            self.assertTrue(dialog.result["name"])
        finally:
            dialog.destroy()

    def test_old_result_cannot_overwrite_edited_url_or_auto_save(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        source = "https://v.douyin.com/old-author/"
        dialog.url_var.set(source)
        started = threading.Event()
        release = threading.Event()

        def expand(url):
            started.set()
            self.assertTrue(release.wait(2))
            return "https://www.douyin.com/user/old-author"

        try:
            with patch.object(app, "expand_douyin_short_link", side_effect=expand) as expansion:
                dialog.save()
                self.assertTrue(started.wait(1))
                dialog.save()
                dialog.save()
                self.assertEqual(1, expansion.call_count)
                changed = "https://www.douyin.com/user/new-author"
                dialog.url_var.set(changed)
                release.set()
                result = dialog.resolve_result_queue.get(timeout=2)
                dialog.resolve_result_queue.put(result)
                dialog.process_resolve_results()
                self.assertEqual(changed, dialog.url_var.get())
                self.assertEqual("", dialog.profile_url_var.get())
                self.assertIsNone(dialog.result)
        finally:
            release.set()
            dialog.destroy()

    def test_destroyed_dialog_accepts_worker_completion_without_tk_access(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        dialog.url_var.set("https://v.douyin.com/author/")
        started = threading.Event()
        release = threading.Event()
        result_queue = dialog.resolve_result_queue

        def expand(url):
            started.set()
            self.assertTrue(release.wait(2))
            return "https://www.douyin.com/user/author"

        try:
            with patch.object(app, "expand_douyin_short_link", side_effect=expand):
                dialog.save()
                self.assertTrue(started.wait(1))
                dialog.destroy()
                release.set()
                result = result_queue.get(timeout=2)
                self.assertTrue(result["ok"], result.get("error"))
                result_queue.put(result)
                dialog.process_resolve_results()
                self.assertIsNone(dialog.result)
        finally:
            release.set()
            dialog.destroy()

    def test_failed_resolution_keeps_the_dialog_open_without_saving(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        source = "https://v.douyin.com/author/"
        dialog.url_var.set(source)
        try:
            with patch.object(app, "expand_douyin_short_link", side_effect=RuntimeError("Could not resolve")), patch.object(
                app.messagebox, "showerror"
            ) as error:
                dialog.save()
                result = dialog.resolve_result_queue.get(timeout=2)
                dialog.resolve_result_queue.put(result)
                dialog.process_resolve_results()
                self.assertIsNone(dialog.result)
                self.assertFalse(dialog.resolving)
                self.assertEqual(source, dialog.url_var.get())
                self.assertTrue(dialog.winfo_exists())
                error.assert_called_once()
        finally:
            dialog.destroy()

    def test_save_edited_short_link_waits_for_the_previous_worker(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        old = "https://v.douyin.com/old-author/"
        new = "https://v.douyin.com/new-author/"
        dialog.url_var.set(old)
        started = threading.Event()
        release = threading.Event()

        def expand(url):
            if url == old:
                started.set()
                self.assertTrue(release.wait(2))
            return "https://www.douyin.com/user/" + ("old-author" if url == old else "new-author")

        try:
            with patch.object(app, "expand_douyin_short_link", side_effect=expand) as expansion:
                dialog.save()
                self.assertTrue(started.wait(1))
                dialog.url_var.set(new)
                dialog.save()
                self.assertEqual(1, expansion.call_count)
                release.set()
                old_result = dialog.resolve_result_queue.get(timeout=2)
                dialog.resolve_result_queue.put(old_result)
                dialog.process_resolve_results()
                new_result = dialog.resolve_result_queue.get(timeout=2)
                dialog.resolve_result_queue.put(new_result)
                dialog.process_resolve_results()
                self.assertEqual(2, expansion.call_count)
                self.assertEqual("https://www.douyin.com/user/new-author", dialog.result["url"])
        finally:
            release.set()
            dialog.destroy()

    def test_short_video_link_does_not_query_the_live_room_api(self):
        dialog = ProfileDialog(self.root, FakeStore())
        dialog.withdraw()
        dialog.url_var.set("https://v.douyin.com/video/")
        try:
            with patch.object(app, "expand_douyin_short_link", return_value="https://www.douyin.com/video/7000000000000000001"), patch.object(
                app.ProfileDialog, "resolve_room", side_effect=AssertionError("A post is not a live room")
            ) as resolve_room, patch.object(app.messagebox, "showerror"):
                dialog.save()
                result = dialog.resolve_result_queue.get(timeout=2)
                dialog.resolve_result_queue.put(result)
                dialog.process_resolve_results()
                resolve_room.assert_not_called()
                self.assertIsNone(dialog.result)
        finally:
            dialog.destroy()

    def test_editing_url_discards_the_previous_room_cache(self):
        profile = {
            "id": "edit-cache", "name": "Test", "url": "https://live.douyin.com/old",
            "fallback_live_url": "https://live.douyin.com/old",
            "fallback_source_url": "https://live.douyin.com/old",
        }
        root = self.root
        try:
            dialog = ProfileDialog(root, FakeStore(), profile)
            dialog.withdraw()
            dialog.url_var.set("https://www.douyin.com/user/new")
            dialog.save()
            self.assertEqual("", dialog.result["fallback_live_url"])
            self.assertEqual("", dialog.result["fallback_source_url"])
        finally:
            dialog.destroy()

    def test_save_preserves_media_profile_url_and_options(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            sec_uid = "MS4wLjABAAAA-test-profile"
            profile_url = f"https://www.douyin.com/user/{sec_uid}"
            profile = {
                "id": "test-profile",
                "enabled": True,
                "record_live": False,
                "priority": False,
                "name": "Test Profile",
                "url": "https://live.douyin.com/123456",
                "original_profile_url": profile_url,
                "output_dir": str(Path(temporary_directory) / "output"),
                "quality": "OD",
                "poll_interval_seconds": 30,
                "media_poll_interval_seconds": 300,
                "auto_download_videos": True,
                "auto_download_stories": True,
                "platform": "douyin",
            }
            root = self.root
            try:
                dialog = ProfileDialog(root, FakeStore(), profile)
                dialog.withdraw()
                dialog.save()
                result = dialog.result
            finally:
                dialog.destroy()

        self.assertEqual(profile_url, result["original_profile_url"])
        self.assertFalse(result["record_live"])
        self.assertTrue(result["auto_download_videos"])
        self.assertTrue(result["auto_download_stories"])
        self.assertEqual(300, result["media_poll_interval_seconds"])


if __name__ == "__main__":
    unittest.main()
