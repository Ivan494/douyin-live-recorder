import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import douyin_recorder_app as app


class FakeStore:
    def __init__(self):
        self.settings = {
            "quality": "OD",
            "container": "mkv",
            "ffmpeg_path": "ffmpeg.exe",
            "new_profile_poll_interval_seconds": 60,
            "media_poll_interval_seconds": 300,
            "recording_stall_timeout_seconds": 300,
            "recording_offline_grace_seconds": 90,
            "recording_segment_max_seconds": 2700,
            "recording_reconnect_delay_max_seconds": 5,
        }
        self.profiles = []

    def save(self):
        pass


class ReliabilityTest(unittest.TestCase):
    def test_gui_logging_does_not_attach_inherited_stdout(self):
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            app, "APP_LOG_FILE", Path(temporary_directory) / "app.log"
        ), patch.object(app, "RotatingFileHandler", return_value=Mock()), patch.object(
            app.logging, "StreamHandler"
        ) as stream_handler, patch.object(app.logging, "basicConfig"):
            app.setup_logging()

        stream_handler.assert_not_called()

    def test_check_mode_logging_attaches_stdout(self):
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            app, "APP_LOG_FILE", Path(temporary_directory) / "app.log"
        ), patch.object(app, "RotatingFileHandler", return_value=Mock()), patch.object(
            app.logging, "StreamHandler"
        ) as stream_handler, patch.object(app.logging, "basicConfig"):
            app.setup_logging(console=True)

        stream_handler.assert_called_once_with(app.sys.stdout)

    def test_default_settings_has_no_startup_process_side_effect(self):
        with patch.object(app, "is_autostart_enabled", side_effect=AssertionError("must not run")):
            settings = app.default_settings()

        self.assertFalse(settings["start_with_windows"])
        self.assertEqual("mkv", settings["container"])
        self.assertEqual("zh-CN", settings["language"])

    def test_pid_probe_uses_process_identity(self):
        self.assertTrue(app.pid_is_running(os.getpid()))
        self.assertFalse(app.pid_is_running(2_000_000_000))

    def test_pid_probe_rejects_a_process_created_after_the_lock(self):
        unix_timestamp = 1_700_000_000
        creation_time = 116444736000000000 + (unix_timestamp * 10_000_000)

        with patch.object(app, "_windows_process_state", return_value=(True, creation_time)):
            self.assertTrue(app.pid_is_running(123, not_started_after=unix_timestamp + 1))
            self.assertFalse(app.pid_is_running(123, not_started_after=unix_timestamp - 1))

    def test_adoption_rejects_sibling_directory_prefix(self):
        engine = app.MonitorEngine(FakeStore(), app.queue.Queue())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            intended = root / "profile"
            sibling = root / "profile-other"
            intended.mkdir()
            sibling.mkdir()
            output = sibling / "recording.flv"
            command = f'ffmpeg.exe -i "https://example.test/live.flv" "{output}"'

            found = engine._output_from_command_line(command, intended)

        self.assertEqual("", found)

    def test_ffmpeg_log_handle_closes_when_start_fails(self):
        engine = app.MonitorEngine(FakeStore(), app.queue.Queue())
        stream = SimpleNamespace(
            anchor_name="Anchor",
            title="Title",
            flv_url="https://example.test/live.flv",
            record_url="",
            m3u8_url="",
        )
        log_handle = Mock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = {"id": "test", "name": "Test", "output_dir": temporary_directory}
            with patch.object(engine, "_save_recording_manifest"), patch.object(
                app, "open", return_value=log_handle
            ), patch.object(
                app.subprocess, "Popen", side_effect=OSError("start failed")
            ):
                with self.assertRaises(OSError):
                    engine._start_recording(profile, stream)

        log_handle.close.assert_called_once_with()

    def test_recording_command_reconnects_transient_http_drops_but_not_true_eof(self):
        engine = app.MonitorEngine(FakeStore(), app.queue.Queue())
        stream = SimpleNamespace(
            anchor_name="Anchor",
            title="Title",
            flv_url="https://example.test/live.flv",
            record_url="",
            m3u8_url="",
        )
        process = SimpleNamespace(pid=1234, poll=lambda: None)
        log_handle = Mock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = {"id": "test", "name": "Test", "output_dir": temporary_directory}
            with patch.object(engine, "_save_recording_manifest"), patch.object(
                app, "open", return_value=log_handle
            ), patch.object(
                app.subprocess, "Popen", return_value=process
            ) as popen:
                engine._start_recording(profile, stream)

        command = popen.call_args.args[0]
        self.assertIn("-reconnect", command)
        self.assertEqual("1", command[command.index("-reconnect") + 1])
        self.assertIn("-reconnect_streamed", command)
        self.assertNotIn("-reconnect_at_eof", command)
        self.assertIn("+discardcorrupt+genpts", command)
        self.assertEqual("5", command[command.index("-reconnect_delay_max") + 1])
        self.assertEqual("0:v?", command[command.index("-map") + 1])
        self.assertIn("0:a?", command)
        self.assertTrue(command[-1].endswith(".mkv"))
        self.assertEqual(
            command.index("-reconnect"),
            command.index("-i") - len(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]),
        )

    def test_unexpected_part_exit_keeps_recording_session_for_url_refresh(self):
        engine = app.MonitorEngine(FakeStore(), app.queue.Queue())
        process = SimpleNamespace(returncode=1, poll=lambda: 1)
        with tempfile.TemporaryDirectory() as temporary_directory:
            part = Path(temporary_directory) / "part-0001.mkv"
            part.write_bytes(b"part")
            engine.processes["test"] = process
            engine.recordings["test"] = {
                "profile_id": "test",
                "session_dir": temporary_directory,
                "manifest_path": str(Path(temporary_directory) / "session.json"),
                "final_output": str(Path(temporary_directory) / "final.mkv"),
                "output_file": str(part),
                "stderr_path": "",
                "started_at": time.time(),
                "parts": [str(part)],
                "part_index": 1,
            }
            with patch.object(engine, "_save_recording_manifest"):
                engine._poll_processes()

        self.assertNotIn("test", engine.processes)
        self.assertIn("test", engine.recordings)
        self.assertEqual("recovering", engine.recordings["test"]["status"])
        self.assertEqual(0, engine.next_check["test"])

    def test_real_ffmpeg_finalizer_stitches_and_validates_mkv_parts(self):
        if not app.DEFAULT_FFMPEG_PATH.exists() or not app.DEFAULT_FFPROBE_PATH.exists():
            self.skipTest("bundled FFmpeg tools are unavailable")
        store = FakeStore()
        store.settings["ffmpeg_path"] = str(app.DEFAULT_FFMPEG_PATH)
        engine = app.MonitorEngine(store, app.queue.Queue())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_dir = root / "session"
            session_dir.mkdir()
            parts = []
            for index, color in enumerate(("red", "blue"), start=1):
                part = session_dir / f"part-{index:04d}.mkv"
                result = app.subprocess.run(
                    [
                        str(app.DEFAULT_FFMPEG_PATH),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=160x120:r=10",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=48000:cl=stereo",
                        "-t",
                        "0.5",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        str(part),
                    ],
                    stdout=app.subprocess.DEVNULL,
                    stderr=app.subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                parts.append(str(part))
            final_output = root / "final.mkv"
            recording = {
                "profile_id": "smoke",
                "profile_name": "Smoke",
                "final_output": str(final_output),
                "session_dir": str(session_dir),
                "manifest_path": str(session_dir / "session.json"),
                "stderr_path": str(root / "ffmpeg.log"),
                "started_at": time.time(),
                "parts": parts,
                "part_index": 2,
                "status": "recovering",
            }

            engine._finalize_recording_worker(recording, "test")

            self.assertTrue(final_output.exists())
            self.assertGreater(final_output.stat().st_size, 0)
            self.assertFalse(session_dir.exists())

    def test_real_segmented_recording_lifecycle_recovers_and_produces_one_mkv(self):
        if not app.DEFAULT_FFMPEG_PATH.exists() or not app.DEFAULT_FFPROBE_PATH.exists():
            self.skipTest("bundled FFmpeg tools are unavailable")
        store = FakeStore()
        store.settings["ffmpeg_path"] = str(app.DEFAULT_FFMPEG_PATH)
        engine = app.MonitorEngine(store, app.queue.Queue())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sources = []
            for index, color in enumerate(("yellow", "green"), start=1):
                source = root / f"source-{index}.flv"
                result = app.subprocess.run(
                    [
                        str(app.DEFAULT_FFMPEG_PATH),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=160x120:r=10",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=48000:cl=stereo",
                        "-t",
                        "0.5",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-f",
                        "flv",
                        str(source),
                    ],
                    stdout=app.subprocess.DEVNULL,
                    stderr=app.subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                sources.append(source)
            profile = {
                "id": "lifecycle",
                "name": "Lifecycle",
                "url": "https://live.douyin.com/123",
                "output_dir": str(root / "output"),
                "container": "mkv",
            }
            for index, source in enumerate(sources, start=1):
                stream = SimpleNamespace(
                    anchor_name="Anchor",
                    title="Title",
                    flv_url=str(source),
                    record_url="",
                    m3u8_url="",
                )
                engine._start_recording(profile, stream)
                engine.processes[profile["id"]].wait(timeout=15)
                engine._poll_processes()
                self.assertIn(profile["id"], engine.recordings)
                self.assertEqual(index, len(engine.recordings[profile["id"]]["parts"]))
                self.assertEqual("recovering", engine.recordings[profile["id"]]["status"])

            recording = engine.recordings[profile["id"]]
            final_output = Path(recording["final_output"])
            session_dir = Path(recording["session_dir"])
            engine._finalize_recording_session(profile["id"], "test lifecycle complete")
            for thread in engine.finalizer_threads:
                thread.join(timeout=30)

            self.assertTrue(final_output.exists())
            self.assertGreater(final_output.stat().st_size, 0)
            self.assertFalse(session_dir.exists())
            probe = app.subprocess.run(
                [
                    str(app.DEFAULT_FFPROBE_PATH),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(final_output),
                ],
                text=True,
                stdout=app.subprocess.PIPE,
                stderr=app.subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(0, probe.returncode, probe.stderr)
            self.assertGreater(float(probe.stdout.strip()), 0.8)

    def test_confirmed_offline_finalizes_existing_mkv_session(self):
        store = FakeStore()
        engine = app.MonitorEngine(store, app.queue.Queue())
        profile = {
            "id": "test",
            "name": "Test",
            "url": "https://live.douyin.com/123",
            "output_dir": "unused",
        }
        engine.recordings["test"] = {
            "profile_id": "test",
            "session_dir": "session",
            "manifest_path": "session/session.json",
            "final_output": "final.mkv",
            "output_file": "part.mkv",
            "stderr_path": "",
            "started_at": time.time() - 120,
            "parts": ["part.mkv"],
            "offline_since": time.time() - 100,
            "offline_confirmations": 1,
        }
        room = {"live_url": profile["url"]}
        stream = SimpleNamespace(is_live=False)
        with patch.object(engine, "_resolve", AsyncMock(return_value=(room, stream))), patch.object(
            engine, "_save_recording_manifest"
        ), patch.object(engine, "_finalize_recording_session") as finalize:
            engine._check_profile(profile)

        finalize.assert_called_once_with("test", "Live confirmed offline")

    def test_profile_normalization_migrates_existing_entries_to_mkv(self):
        store = object.__new__(app.RecorderStore)
        store.settings = {"container": "flv", "recording_engine_version": 1}
        store.profiles = [{"name": "Test", "url": "https://live.douyin.com/123456", "container": "flv"}]

        migrated = store.normalize()

        self.assertTrue(migrated)
        self.assertEqual("mkv", store.settings["container"])
        self.assertEqual("mkv", store.profiles[0]["container"])

    def test_quality_choices_match_bundled_resolver(self):
        self.assertEqual(("OD", "UHD", "HD", "SD", "LD"), app.QUALITY_OPTIONS)
        self.assertNotIn("BD", app.QUALITY_OPTIONS)

    def test_launcher_does_not_wait_for_entire_gui_lifetime(self):
        source = (app.APP_DIR / "DouyinLiveRecorderLauncher.cs").read_text(encoding="utf-8")

        self.assertNotIn("child.WaitForExit();", source)
        self.assertIn("child.WaitForExit(ChildStartupWaitMilliseconds)", source)

    def test_main_table_keeps_only_operational_columns(self):
        self.assertEqual(
            (
                "enabled",
                "name",
                "status",
                "media_auto",
                "media_progress",
                "next_check",
            ),
            app.PROFILE_TABLE_COLUMNS,
        )

    def test_main_table_columns_fit_supported_minimum_width(self):
        with patch.object(app.RecorderStore, "save"), patch.object(
            app.RecorderApp, "_start_tray"
        ), patch.object(app.RecorderApp, "hide_to_tray"), patch.object(
            app.RecorderApp, "refresh_session_status"
        ):
            recorder = app.RecorderApp()

        try:
            minimum_width, minimum_height = recorder.root.minsize()
            recorder.root.geometry(f"{minimum_width}x{minimum_height}+20+20")
            recorder.root.deiconify()
            recorder.root.update()

            column_width = sum(
                recorder.tree.column(column, "width")
                for column in app.PROFILE_TABLE_COLUMNS
            )

            self.assertLessEqual(column_width, recorder.tree.winfo_width())
        finally:
            recorder.root.destroy()

    def test_media_progress_text_covers_scan_transfer_and_retry(self):
        from i18n import set_language

        set_language("en")
        self.assertEqual(
            "Scanning history • 3 pages • 54 found",
            app.media_progress_text({"phase": "scanning", "pages": 3, "found": 54}),
        )
        self.assertIn(
            "2.0 MB / 8.0 MB",
            app.media_progress_text(
                {
                    "phase": "downloading",
                    "media_kind": "video",
                    "current": 4,
                    "total": 20,
                    "bytes_downloaded": 2 * 1024 * 1024,
                    "bytes_total": 8 * 1024 * 1024,
                }
            ),
        )
        self.assertIn(
            "retry 2/5",
            app.media_progress_text(
                {
                    "phase": "retrying",
                    "media_kind": "video",
                    "current": 4,
                    "total": 20,
                    "attempt": 2,
                    "attempts": 5,
                }
            ),
        )

    def test_media_summary_does_not_claim_antibot_or_session_health(self):
        from i18n import set_language

        set_language("en")
        summary = {"videos": {"status": "api_empty"}}

        message = app.MediaDownloadEngine.summarize_kind(summary, "videos")

        self.assertEqual("Works: API returned an empty response", message)
        self.assertNotIn("anti-bot", message.lower())
        self.assertNotIn("session ok", message.lower())

    def test_media_summary_surfaces_mobile_only_story(self):
        from i18n import set_language

        set_language("en")
        summary = {"stories": {"status": "mobile_only"}}

        self.assertEqual(
            "Stories: active, mobile app access required",
            app.MediaDownloadEngine.summarize_kind(summary, "stories"),
        )

    def test_media_summary_explains_profile_specific_work_gate(self):
        summary = {"videos": {"status": "login_required"}}

        self.assertEqual(
            "Works: Douyin requires login for this profile (it may contain notes only)",
            app.MediaDownloadEngine.summarize_kind(summary, "videos"),
        )

    def test_profile_normalization_preserves_story_auto_download(self):
        store = object.__new__(app.RecorderStore)
        store.settings = app.default_settings()
        store.profiles = [{"name": "Test", "url": "https://live.douyin.com/123456", "auto_download_stories": True}]

        store.normalize()

        self.assertTrue(store.profiles[0]["auto_download_stories"])

    def test_media_worker_runs_story_only_profiles(self):
        self.assertTrue(app.MediaDownloadEngine.enabled_for_profile({"auto_download_stories": True}))


class ProfileResolverTest(unittest.TestCase):
    def test_douyin_live_resolution_never_receives_profile_cookies(self):
        engine = app.MonitorEngine(FakeStore(), app.queue.Queue())
        profile = {
            "url": "https://live.douyin.com/123456",
            "cookies": "profile-cookie=secret",
            "stream_orientation": 1,
        }
        room = {"anchor_name": "Anchor"}
        stream = SimpleNamespace(is_live=False)
        live = Mock()
        live.fetch_web_stream_data = AsyncMock(return_value=room)
        live.fetch_stream_url = AsyncMock(return_value=stream)

        with patch.object(app, "DouyinLiveStream", return_value=live) as live_type:
            resolved_room, resolved_stream = app.asyncio.run(engine._resolve_douyin(profile))

        self.assertIs(room, resolved_room)
        self.assertIs(stream, resolved_stream)
        self.assertIsNone(live_type.call_args.kwargs["cookies"])

    def test_resolve_network_work_does_not_block_tk(self):
        class Value:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Button:
            def configure(self, **_kwargs):
                pass

        dialog = SimpleNamespace(
            resolving=False,
            url_var=Value("https://live.douyin.com/123456"),
            profile_url_var=Value(),
            quality_var=Value("OD"),
            name_var=Value(),
            store=FakeStore(),
            resolve_button=Button(),
            resolve_result_queue=app.queue.Queue(),
        )

        async def delayed_resolve(_url, _quality=None):
            await app.asyncio.sleep(0.25)
            return {"anchor_name": "Resolved", "live_url": "https://live.douyin.com/123456"}, SimpleNamespace()

        dialog.resolve_room = delayed_resolve
        started = time.monotonic()
        app.ProfileDialog.resolve_link(dialog)
        returned_in = time.monotonic() - started
        result = dialog.resolve_result_queue.get(timeout=2)

        self.assertLess(returned_in, 0.1)
        self.assertTrue(result["ok"])
        self.assertEqual("Resolved", result["room"]["anchor_name"])


if __name__ == "__main__":
    unittest.main()
