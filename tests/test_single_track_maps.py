"""Recorder and finalizer must map only the first video + first audio stream.
Douyin FLV can expose phantom empty extra streams; mapping every stream copied
them and produced 2v+2a MKVs (regression seen 2026-10-15)."""
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SRC = (Path(__file__).resolve().parents[1] / "src" / "douyin_recorder_app.py").read_text(encoding="utf-8")


class SingleTrackMaps(unittest.TestCase):
    def test_no_all_stream_maps(self):
        self.assertNotIn('"0:v?"', SRC)
        self.assertNotIn('"0:a?"', SRC)
        self.assertGreaterEqual(SRC.count('"0:v:0?"'), 2)
        self.assertGreaterEqual(SRC.count('"0:a:0?"'), 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg missing")
    def test_concat_of_2v2a_parts_yields_1v1a(self):
        from douyin_headless import app

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            session = d / ".recording_sessions" / "fixture"
            session.mkdir(parents=True)
            parts = []
            for i in range(2):
                p = session / f"part{i}.mkv"
                subprocess.run(["ffmpeg", "-v", "error", "-y",
                                "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
                                "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
                                "-f", "lavfi", "-i", "sine=duration=1",
                                "-f", "lavfi", "-i", "sine=duration=1",
                                "-map", "0", "-map", "1", "-map", "2", "-map", "3",
                                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(p)], check=True, timeout=30)
                parts.append(p)
            out = d / "out.mkv"
            store = SimpleNamespace(settings={"ffmpeg_path": shutil.which("ffmpeg"),
                                              "ffprobe_path": shutil.which("ffprobe")}, profiles=[])
            engine = app.MonitorEngine(store, app.RecorderEventQueue())
            recording = {"profile_id": "fixture", "session_dir": str(session),
                         "manifest_path": str(session / "session.json"),
                         "parts": [str(p) for p in parts], "final_output": str(out)}
            engine._finalize_recording_worker(recording, "offline 2v2a fixture")
            self.assertEqual(recording["status"], "complete")
            self.assertFalse(session.exists())
            info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                              "-of", "json", str(out)], capture_output=True, text=True, check=True, timeout=30).stdout)
            self.assertEqual(sorted(s["codec_type"] for s in info["streams"]), ["audio", "video"])


if __name__ == "__main__":
    unittest.main()
