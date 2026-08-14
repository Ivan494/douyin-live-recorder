import os
import tempfile
import unittest
from pathlib import Path
from tkinter import Tk

from douyin_recorder_app import ProfileDialog


class FakeStore:
    settings = {
        "quality": "OD",
        "new_profile_poll_interval_seconds": 60,
        "media_poll_interval_seconds": 300,
    }


@unittest.skipUnless(os.name == "nt", "Tk profile dialog smoke test is Windows-only")
class ProfileDialogTest(unittest.TestCase):
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
            root = Tk()
            root.withdraw()
            try:
                dialog = ProfileDialog(root, FakeStore(), profile)
                dialog.withdraw()
                dialog.save()
                result = dialog.result
            finally:
                root.destroy()

        self.assertEqual(profile_url, result["original_profile_url"])
        self.assertFalse(result["record_live"])
        self.assertTrue(result["auto_download_videos"])
        self.assertTrue(result["auto_download_stories"])
        self.assertEqual(300, result["media_poll_interval_seconds"])


if __name__ == "__main__":
    unittest.main()
