"""Exercise real FFmpeg capture and publication through the public recorder."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import douyin_recorder_app as app


def test_real_recording_and_finalization(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required")
    source = tmp_path / "source.mkv"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, timeout=30)
    # Only this synthetic fixture accepts a local file as its live source.
    monkeypatch.setattr(app, "is_safe_recording_url", lambda url: url == str(source))
    monkeypatch.setattr(app, "recording_input_url", lambda stream: (str(source), "fixture"))
    profile = {"id": "fixture", "name": "Fixture", "enabled": True,
               "record_live": True, "output_dir": str(tmp_path / "capture"), "container": "mkv"}
    settings = app.default_settings()
    settings.update(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, adopt_existing_ffmpeg=False)
    store = SimpleNamespace(
        settings=settings, profiles=[profile], lock=threading.RLock(),
        get_profile=lambda profile_id: profile if profile_id == "fixture" else None,
    )
    engine = app.MonitorEngine(store, app.RecorderEventQueue())
    try:
        stream = SimpleNamespace(title="Integration", anchor_name="Fixture", is_live=True)
        engine._start_recording(profile, stream)
        recording = engine.recordings["fixture"]
        final = Path(recording["final_output"])
        assert engine.processes["fixture"].wait(timeout=30) == 0
        engine.stop(terminate_recordings=True)
        for finalizer in list(engine.finalizer_threads):
            finalizer.join(timeout=30)
        assert final.is_file() and final.stat().st_size > 10000
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(final)],
            check=True, capture_output=True, text=True, timeout=20,
        )
        probe = json.loads(result.stdout)
        assert {item["codec_type"] for item in probe["streams"]} >= {"video", "audio"}
        assert float(probe["format"]["duration"]) >= 1.8
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(final), "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        assert decode.returncode == 0 and decode.stderr == ""
    finally:
        engine.stop(terminate_recordings=True)
