"""Real media through same-pass exit, failed input, reconnect and cold recovery."""
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import douyin_recorder_app as app


@pytest.mark.parametrize("lose_first_part", [False, True])
def test_capture_reconnect_and_cold_recovery_preserve_media(tmp_path, monkeypatch, lose_first_part):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required")
    source = tmp_path / "source.mkv"
    unavailable = tmp_path / "unavailable.mkv"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, timeout=30)
    # Local media is allowed only inside this isolated capture fixture.
    monkeypatch.setattr(app, "is_safe_recording_url", lambda url: url in {str(source), str(unavailable)})
    monkeypatch.setattr(app, "recording_input_url", lambda stream: (stream.flv_url, "fixture"))
    monkeypatch.setattr(app, "has_recording_url", lambda stream: bool(stream.flv_url))
    profile = {"id": "lifecycle", "name": "Fixture", "url": "https://live.douyin.com/123",
               "enabled": True, "record_live": True, "output_dir": str(tmp_path / "capture")}
    settings = app.default_settings()
    settings.update(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, adopt_existing_ffmpeg=False)
    store = SimpleNamespace(settings=settings, profiles=[profile], lock=threading.RLock(),
                            get_profile=lambda _profile_id: profile)
    engine = app.MonitorEngine(store, app.RecorderEventQueue())
    stream = SimpleNamespace(title="Lifecycle", anchor_name="Fixture", is_live=True, flv_url=str(source))
    try:
        engine._apply_profile_result(profile, {}, stream)
        first = engine.processes[profile["id"]]
        first_stderr = engine.stderr_files[profile["id"]]
        recording = engine.recordings[profile["id"]]
        assert first.wait(timeout=30) == 0
        recording["segment_started_at"] = time.time() - 31
        recording["rotation_stop_requested"] = True
        # The profile result arrives before the supervisor reaps the prior exit.
        stream.flv_url = str(unavailable)
        engine._apply_profile_result(profile, {}, stream)
        assert first_stderr.closed
        assert "rotation_stop_requested" not in recording
        assert recording["part_index"] == 2
        second = engine.processes[profile["id"]]
        assert second.wait(timeout=30) != 0
        missing = Path(recording["output_file"])
        assert not missing.exists()
        stream.flv_url = str(source)
        engine._apply_profile_result(profile, {}, stream)
        assert profile["id"] not in engine.processes
        assert engine.recording_error_counts[profile["id"]] == 1
        assert str(missing) not in recording["parts"]
        assert engine.next_check[profile["id"]] > time.monotonic()
        # Simulate the next scheduled profile check after that backoff.
        engine.next_check[profile["id"]] = 0
        engine._apply_profile_result(profile, {}, stream)
        assert recording["part_index"] == 3
        assert engine.processes[profile["id"]].wait(timeout=30) == 0
        parts = [Path(p) for p in recording["parts"]]
        assert [p.name for p in parts] == ["part-0001.flv", "part-0003.flv"]
        for handle in engine.stderr_files.values():
            handle.close()
        if lose_first_part:
            parts[0].unlink()
        recovered_engine = app.MonitorEngine(store, app.RecorderEventQueue())
        recovered_engine.recover_pending_recording_sessions()
        recovered = recovered_engine.recordings[profile["id"]]
        assert recovered["parts"] == [str(p) for p in parts]
        assert recovered["part_index"] == 3
        recovered_engine._finalize_recording_worker(recovered, "fixture offline after restart")
        final = Path(recovered["final_output"])
        if lose_first_part:
            assert recovered["status"] == "failed"
            assert parts[1].is_file() and Path(recovered["manifest_path"]).is_file()
            assert not final.exists()
        else:
            assert recovered["status"] == "complete", recovered.get("stop_reason")
            result = subprocess.run([
                ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(final),
            ], check=True, capture_output=True, text=True, timeout=30)
            probe = json.loads(result.stdout)
            video = next(s for s in probe["streams"] if s["codec_type"] == "video")
            assert int(video["nb_read_frames"]) == 40
            assert {s["codec_type"] for s in probe["streams"]} >= {"video", "audio"}
            assert float(probe["format"]["duration"]) >= 3.8
            decode = subprocess.run([ffmpeg, "-v", "error", "-i", str(final), "-f", "null", "-"],
                                    capture_output=True, text=True, timeout=30)
            assert decode.returncode == 0 and decode.stderr == ""
    finally:
        for process in engine.processes.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        for handle in engine.stderr_files.values():
            handle.close()
