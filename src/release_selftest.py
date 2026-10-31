"""Offline frozen-build smoke test; never loads accounts or starts monitoring."""
import gc
import json
from pathlib import Path
import subprocess
import re
import sys
import tempfile
from types import SimpleNamespace


def recording_self_test(app_dir):
    """Exercise the packaged finalizer with tiny synthetic PK captures.

    No profiles, login state, network requests or existing recordings are used.
    Comparing decoded pixel checksums catches silent HEVC misdecoding that a
    successful FFmpeg exit and a stream listing do not detect.
    """
    from recording_segments import _read_flv_header, _iter_tags, _write_tag, ffmpeg_live_capture_output_args
    app = sys.modules.get("__main__")
    if not hasattr(app, "MonitorEngine"):
        import douyin_recorder_app as app
    tools = Path(app_dir).parent.parent / "youtube-dl"
    ffmpeg, ffprobe = tools / "ffmpeg.exe", tools / "ffprobe.exe"

    def run(*args):
        result = subprocess.run([str(ffmpeg), "-hide_banner", *map(str, args)],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Recording self-test FFmpeg failed: " + result.stderr[-2000:])
        return result.stderr

    def frames(path):
        log = run("-v", "info", "-i", path, "-map", "0:v:0", "-vf", "showinfo", "-f", "null", "-")
        return re.findall(r"Parsed_showinfo.*?s:(\d+x\d+).*?checksum:([0-9A-F]+)", log)

    counts = {}
    with tempfile.TemporaryDirectory(prefix="录制编码验证 ") as temporary:
        root = Path(temporary)
        for codec in ("libx264", "libx265"):
            directory = root / codec
            directory.mkdir()
            inputs = []
            for index, size in enumerate(("160x120", "192x144")):
                path = directory / f"input-{index}.flv"
                run("-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=16",
                    "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "0.5",
                    "-c:v", codec, "-preset", "ultrafast", "-bf", "2",
                    *(["-x265-params", "log-level=error"] if codec == "libx265" else []),
                    "-c:a", "aac", "-f", "flv", path)
                inputs.append(path)
            spliced = directory / "live.flv"
            with spliced.open("wb") as dest:
                for index, path in enumerate(inputs):
                    with path.open("rb") as source:
                        header = _read_flv_header(source)
                        if index == 0:
                            dest.write(header + b"\0" * 4)
                        for kind, timestamp, body in _iter_tags(source):
                            if kind != 18:
                                _write_tag(dest, kind, timestamp + index * 1000, body)
            session = directory / "session"
            session.mkdir()
            capture = session / "part-0001.flv"
            run("-v", "error", "-y", "-i", spliced, *ffmpeg_live_capture_output_args(capture))
            expected = [frame for path in inputs for frame in frames(path)]
            if len(expected) != 16 or frames(capture) != expected:
                raise RuntimeError(f"{codec} capture lost or changed frames")
            output = directory / "final.mkv"
            store = SimpleNamespace(settings={"ffmpeg_path": str(ffmpeg), "ffprobe_path": str(ffprobe)})
            engine = app.MonitorEngine(store, app.queue.Queue())
            recording = {"profile_id": "offline-self-test", "session_dir": str(session),
                         "parts": [str(capture)], "final_output": str(output),
                         "manifest_path": str(session / "session.json")}
            engine._finalize_recording_worker(recording, "offline release self-test")
            if recording.get("status") != "complete" or frames(output) != expected:
                raise RuntimeError(f"{codec} finalization changed or lost frames: {recording.get('stop_reason')}")
            if run("-v", "error", "-i", output, "-f", "null", "-"):
                raise RuntimeError(f"{codec} finished recording has decoding errors")
            counts[codec] = len(expected)
    return counts


def run_self_test(report_path, app_dir):
    result = {"ok": False, "checks": []}
    try:
        from tkinter import Tk, ttk
        import douyin_media_downloader as media
        from signer.gorgon import get_xgorgon
        from signer.argus import Argus
        from signer.ladon import Ladon
        from streamget import JS_SCRIPT_PATH

        app_dir = Path(app_dir)
        if media.APP_DIR != app_dir:
            raise RuntimeError("Frozen application and media state paths disagree")
        result["checks"].append("persistent-paths")
        if not (Path(JS_SCRIPT_PATH) / "x-bogus.js").is_file():
            raise RuntimeError("Missing streamget resources")
        query = "device_id=0&version_name=1.0.0"
        assert get_xgorgon(query, 1700000000)
        assert Argus.get_sign(queryhash=query, timestamp=1700000000, aid=1128)
        assert Ladon.encrypt(1700000000, "1611921764", 1128)
        result["checks"].append("signing-and-resources")
        root = Tk()
        root.withdraw()
        frame = ttk.Frame(root)
        ttk.Label(frame, text="发布自检").pack()
        frame.pack()
        root.update_idletasks()
        root.destroy()
        del frame, root
        gc.collect()
        result["checks"].append("tk-interface")
        for name in ("ffmpeg", "ffprobe"):
            tool = app_dir.parent.parent / "youtube-dl" / (name + ".exe")
            subprocess.run([str(tool), "-version"], check=True, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result["checks"].append(name)
        result["recording_frames"] = recording_self_test(app_dir)
        result["checks"].append("recording-h264-hevc")
        result["ok"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
