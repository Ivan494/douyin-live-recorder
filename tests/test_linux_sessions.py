"""Private-session tests that do not import the desktop GUI or optional libraries."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))
import import_linux_session as importer
import linux_session_storage as storage


SYNTHETIC_HEADER = "sessionid=synthetic-private-cookie; uid_tt=synthetic-user"
SYNTHETIC_DEVICE = {
    "device_id": "7200000000000000001",
    "install_id": "7200000000000000002",
    "cdid": "981a9498-7dc3-49d3-9757-c64c8e91e199",
    "own_uid": "synthetic-account-id",
    "version_code": "380700",
}


@unittest.skipIf(os.name == "nt", "POSIX session permissions require Linux")
class LinuxSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "src"
        self.directory.mkdir(mode=0o755)
        self.session = self.directory / "douyin_session.json"

    def payload(self):
        return {
            "web_cookie_header": SYNTHETIC_HEADER,
            "mobile_cookie_header": "sessionid=synthetic-mobile-cookie",
            "mobile_device": dict(SYNTHETIC_DEVICE),
        }

    def test_round_trip_and_owner_only_permissions(self):
        os.chmod(self.directory, 0o755)
        storage.save_cookie_header(self.session, SYNTHETIC_HEADER)
        self.assertEqual(SYNTHETIC_HEADER, storage.load_cookie_header(self.session))
        self.assertEqual(0o700, stat.S_IMODE(self.directory.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.session.stat().st_mode))
        self.assertEqual("linux-private-v1", json.loads(self.session.read_text())["format"])
        self.assertEqual([], list(self.directory.glob("*.tmp")))

    def test_refuses_group_and_world_readable_files(self):
        storage.save_cookie_header(self.session, SYNTHETIC_HEADER)
        for mode in (0o640, 0o604, 0o644):
            with self.subTest(mode=mode):
                os.chmod(self.session, mode)
                self.assertEqual("", storage.load_cookie_header(self.session))

    def test_refuses_non_private_parent_directory(self):
        storage.save_cookie_header(self.session, SYNTHETIC_HEADER)
        os.chmod(self.directory, 0o750)
        self.assertEqual("", storage.load_cookie_header(self.session))

    def test_rejects_windows_format_without_decrypting_or_echoing_it(self):
        storage.save_private_json(
            self.session,
            {
                "format": "windows-dpapi-v1",
                "cookie_header": SYNTHETIC_HEADER,
                "encrypted_cookie_header": "synthetic-encrypted-value",
            },
        )
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual("", storage.load_cookie_header(self.session))
        self.assertEqual("", output.getvalue())
        self.assertEqual("", errors.getvalue())

    def test_rejects_symlink_session_on_load_and_save(self):
        target = self.root / "outside.json"
        target.write_text("outside-marker", encoding="utf-8")
        self.session.symlink_to(target)
        self.assertEqual("", storage.load_cookie_header(self.session))
        with self.assertRaises(storage.PrivateSessionError):
            storage.save_cookie_header(self.session, SYNTHETIC_HEADER)
        self.assertEqual("outside-marker", target.read_text())
        self.assertTrue(self.session.is_symlink())

    def test_rejects_symlink_parent_directory(self):
        alternate = self.root / "alternate"
        alternate.mkdir(mode=0o700)
        linked = self.root / "linked"
        linked.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(OSError):
            storage.save_cookie_header(linked / "session.json", SYNTHETIC_HEADER)
        self.assertEqual("", storage.load_cookie_header(linked / "session.json"))
        self.assertEqual([], list(alternate.iterdir()))

    def test_failed_serialization_preserves_original_and_cleans_temporary_file(self):
        storage.save_cookie_header(self.session, SYNTHETIC_HEADER)
        original = self.session.read_bytes()
        with self.assertRaises(TypeError):
            storage.save_private_json(self.session, {"not_json": object()})
        self.assertEqual(original, self.session.read_bytes())
        self.assertEqual([self.session], list(self.directory.iterdir()))

    def test_atomic_replace_failure_preserves_original(self):
        storage.save_cookie_header(self.session, SYNTHETIC_HEADER)
        original = self.session.read_bytes()
        with patch.object(storage.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                storage.save_cookie_header(self.session, "sessionid=synthetic-replacement")
        self.assertEqual(original, self.session.read_bytes())
        self.assertEqual([self.session], list(self.directory.iterdir()))

    def test_import_preserves_bound_device_and_distinct_cookie_headers(self):
        result = importer.import_session(self.payload(), self.directory)
        self.assertTrue(all(result.values()))
        self.assertTrue(all(isinstance(value, bool) for value in result.values()))
        device = json.loads((self.directory / "mobile_device.json").read_text())
        self.assertEqual(SYNTHETIC_DEVICE, device)
        self.assertEqual(SYNTHETIC_HEADER, storage.load_cookie_header(self.session))
        self.assertEqual(
            "sessionid=synthetic-mobile-cookie",
            storage.load_cookie_header(self.directory / "mobile_session.json"),
        )
        for path in self.directory.iterdir():
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_web_session_can_supply_mobile_cookie_with_same_original_device(self):
        payload = self.payload()
        payload["mobile_cookie_header"] = ""
        importer.import_session(payload, self.directory)
        self.assertEqual(
            SYNTHETIC_HEADER, storage.load_cookie_header(self.directory / "mobile_session.json")
        )
        device = json.loads((self.directory / "mobile_device.json").read_text())
        self.assertEqual(SYNTHETIC_DEVICE, device)

    def test_invalid_device_is_rejected_before_writing_files(self):
        payload = self.payload()
        payload["mobile_device"]["device_id"] = "invalid"
        with self.assertRaises(storage.PrivateSessionError):
            importer.import_session(payload, self.directory)
        self.assertEqual([], list(self.directory.iterdir()))

    def test_symlink_destination_is_rejected_before_other_session_updates(self):
        target = self.root / "outside.json"
        target.write_text("outside-marker", encoding="utf-8")
        (self.directory / "mobile_session.json").symlink_to(target)
        device_path = self.directory / "mobile_device.json"
        device_path.write_text("original-device", encoding="utf-8")
        with self.assertRaises(storage.PrivateSessionError):
            importer.import_session(self.payload(), self.directory)
        self.assertEqual("outside-marker", target.read_text())
        self.assertEqual("original-device", device_path.read_text())
        self.assertFalse(self.session.exists())

    def test_cli_outputs_only_readiness_and_never_credentials(self):
        result = subprocess.run(
            [sys.executable, str(SOURCE / "import_linux_session.py"), "--directory", str(self.directory)],
            input=json.dumps(self.payload()), text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        readiness = json.loads(result.stdout)
        self.assertEqual({"web_ready": True, "mobile_ready": True, "device_ready": True}, readiness)
        self.assertNotIn("synthetic", result.stdout)
        self.assertNotIn(SYNTHETIC_DEVICE["device_id"], result.stdout)

    def test_malformed_sensitive_input_has_no_diagnostics(self):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = importer.main(
                ["--directory", str(self.directory)],
                input_stream=io.StringIO('{"cookie":"synthetic-secret-not-closed'),
                output_stream=output,
            )
        self.assertEqual(1, code)
        self.assertEqual("", errors.getvalue())
        self.assertTrue(all(value is False for value in json.loads(output.getvalue()).values()))
        self.assertNotIn("synthetic", output.getvalue())


class PlatformBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows platform boundary")
    def test_linux_loader_does_not_read_files_on_windows(self):
        with patch.object(storage, "load_private_json") as load:
            self.assertEqual("", storage.load_cookie_header(Path("unused.json")))
        load.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows platform boundary")
    def test_windows_import_outputs_false_without_sensitive_errors(self):
        output = io.StringIO()
        code = importer.main(
            [], input_stream=io.StringIO(json.dumps({"web_cookie_header": SYNTHETIC_HEADER})),
            output_stream=output,
        )
        self.assertEqual(1, code)
        self.assertTrue(all(value is False for value in json.loads(output.getvalue()).values()))
        self.assertNotIn("synthetic", output.getvalue())


if __name__ == "__main__":
    unittest.main()
