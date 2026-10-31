"""Exercise real Linux FFmpeg through the deployed recording engine."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DOUYIN_HEADLESS"] = "1"
import douyin_recorder_app as app


@pytest.mark.skipif(os.name == "nt", reason="Linux deployment integration")
def test_real_recording_and_finalization(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    subprocess.run([
        "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, timeout=30)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    input_url = f"http://127.0.0.1:{server.server_port}/source.mkv"
    # Only this local test fixture bypasses production public-host validation.
    monkeypatch.setattr(app, "is_safe_recording_url", lambda url: url == input_url)
    monkeypatch.setattr(app, "recording_input_url", lambda stream: (input_url, "http"))
    profile = {
        "id": "fixture", "name": "Fixture", "url": "https://live.douyin.com/fixture",
        "enabled": True, "record_live": True, "output_dir": str(tmp_path / "capture"),
        "container": "mkv", "auto_download_videos": False, "auto_download_stories": True,
    }
    settings = app.default_settings()
    settings.update(ffmpeg_path="/usr/bin/ffmpeg", ffprobe_path="/usr/bin/ffprobe", adopt_existing_ffmpeg=False)
    store = SimpleNamespace(
        settings=settings, profiles=[profile], lock=threading.RLock(),
        get_profile=lambda profile_id: profile if profile_id == "fixture" else None,
    )
    engine = app.MonitorEngine(store, app.RecorderEventQueue())
    try:
        stream = SimpleNamespace(
            title="Integration", anchor_name="Fixture", flv_url=input_url,
            record_url=input_url, is_live=True,
        )
        engine._start_recording(profile, stream)
        recording = engine.recordings["fixture"]
        final = Path(recording["final_output"])
        assert engine.processes["fixture"].wait(timeout=30) == 0
        engine.stop(terminate_recordings=True)
        for finalizer in list(engine.finalizer_threads):
            finalizer.join(timeout=30)
        assert final.is_file() and final.stat().st_size > 10000
        result = subprocess.run(
            [
                "/usr/bin/ffprobe", "-v", "error", "-show_streams",
                "-show_format", "-of", "json", str(final),
            ],
            check=True, capture_output=True, text=True, timeout=20,
        )
        probe = json.loads(result.stdout)
        assert {item["codec_type"] for item in probe["streams"]} >= {"video", "audio"}
        assert float(probe["format"]["duration"]) >= 1.8
        decode = subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        assert decode.returncode == 0 and decode.stderr == ""
    finally:
        engine.stop(terminate_recordings=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
