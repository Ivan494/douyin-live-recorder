import os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DOUYIN_HEADLESS", "1")
import douyin_headless as h
import douyin_recorder_app as app


def test_failed_session_with_parts_is_retried(tmp_path):
    part = tmp_path / "part-0001.mkv"; part.write_bytes(b"x")
    E = h.HeadlessMonitorEngine
    assert E._should_retry_finalize({"status": "failed", "parts": [str(part)]})
    assert not E._should_retry_finalize({"status": "failed", "parts": [str(part)], "finalize_attempts": 3})
    assert not E._should_retry_finalize({"status": "failed", "parts": [str(tmp_path / "gone.mkv")]})
    assert not E._should_retry_finalize({"status": "complete", "parts": [str(part)]})
    assert E._should_retry_finalize({"status": "finalizing", "parts": []})


def test_finalize_timeout_scales_with_size(tmp_path):
    part = tmp_path / "p.mkv"
    with open(part, "wb") as fh:
        fh.truncate(10 << 30)  # sparse 10 GiB
    eng = object.__new__(app.MonitorEngine)
    eng.store = SimpleNamespace(settings={"headless_finalize_timeout_seconds": 240})
    assert eng._finalize_timeout([part]) == 240 + 1200
    assert eng._finalize_timeout([]) == 240


def test_manifest_keeps_attempt_counter():
    assert app.MonitorEngine._manifest_payload({"finalize_attempts": 2})["finalize_attempts"] == 2
