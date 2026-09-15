"""Offline frozen-build smoke test; never loads accounts or starts monitoring."""
import gc
import json
from pathlib import Path
import subprocess


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
        result["ok"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
