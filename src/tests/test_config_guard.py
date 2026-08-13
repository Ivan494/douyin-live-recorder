import json
import tempfile
import unittest
from pathlib import Path

import douyin_recorder_app as app


class ConfigGuardTest(unittest.TestCase):
    """Regression tests for the config-clobber incidents of 2026-08-04.

    A stray process once persisted an empty profile list over the real
    profiles.json. save_json must now refuse to wipe a non-empty config
    with an empty list, while still snapshotting the previous file.
    """

    def test_save_json_refuses_to_empty_a_non_empty_profiles_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "profiles.json"
            original = [{"id": "keep-me", "name": "Test", "url": "https://live.douyin.com/123"}]
            target.write_text(json.dumps(original), encoding="utf-8")

            app.save_json(target, [])

            self.assertEqual(original, json.loads(target.read_text(encoding="utf-8")))

    def test_save_json_keeps_snapshot_before_refusing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "profiles.json"
            target.write_text('[{"id": "keep-me"}]', encoding="utf-8")

            app.save_json(target, [])

            backup_dir = Path(temporary_directory) / "config_backups"
            backups = list(backup_dir.glob("profiles.json.*.bak"))
            self.assertTrue(backups)
            self.assertEqual(
                [{"id": "keep-me"}],
                json.loads(backups[0].read_text(encoding="utf-8")),
            )

    def test_save_json_allows_normal_updates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "profiles.json"
            target.write_text('[{"id": "one"}]', encoding="utf-8")

            app.save_json(target, [{"id": "two"}])

            self.assertEqual([{"id": "two"}], json.loads(target.read_text(encoding="utf-8")))

    def test_save_json_allows_fresh_empty_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "profiles.json"

            app.save_json(target, [])

            self.assertEqual([], json.loads(target.read_text(encoding="utf-8")))

    def test_shipped_settings_do_not_enable_windows_autostart(self):
        settings_path = Path(__file__).resolve().parents[1] / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertFalse(data.get("start_with_windows"))
        self.assertEqual("mkv", data.get("container"))
        self.assertEqual("zh-CN", data.get("language"))

    def test_shipped_profiles_are_empty(self):
        profiles_path = Path(__file__).resolve().parents[1] / "profiles.json"
        data = json.loads(profiles_path.read_text(encoding="utf-8"))
        self.assertEqual([], data)


if __name__ == "__main__":
    unittest.main()
