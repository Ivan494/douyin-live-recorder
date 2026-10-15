"""Recorder and finalizer must map only the first video + first audio stream.
Douyin FLV can expose phantom empty extra streams; mapping every stream copied
them and produced 2v+2a MKVs (regression seen 2026-10-15)."""
import json, shutil, subprocess, tempfile, unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "src" / "douyin_recorder_app.py").read_text(encoding="utf-8")


class SingleTrackMaps(unittest.TestCase):
    def test_no_all_stream_maps(self):
        self.assertNotIn('"0:v?"', SRC)
        self.assertNotIn('"0:a?"', SRC)
        self.assertGreaterEqual(SRC.count('"0:v:0?"'), 2)
        self.assertGreaterEqual(SRC.count('"0:a:0?"'), 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg missing")
    def test_concat_of_2v2a_parts_yields_1v1a(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            parts = []
            for i in range(2):
                p = d / f"part{i}.mkv"
                subprocess.run(["ffmpeg", "-v", "error", "-y",
                                "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
                                "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
                                "-f", "lavfi", "-i", "sine=duration=1",
                                "-f", "lavfi", "-i", "sine=duration=1",
                                "-map", "0", "-map", "1", "-map", "2", "-map", "3",
                                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(p)], check=True)
                parts.append(p)
            lst = d / "parts.txt"
            lst.write_text("".join(f"file '{p}'\n" for p in parts))
            out = d / "out.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", str(out)], check=True)
            info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                              "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
            self.assertEqual(sorted(s["codec_type"] for s in info["streams"]), ["audio", "video"])


if __name__ == "__main__":
    unittest.main()
