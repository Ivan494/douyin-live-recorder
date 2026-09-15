"""Optional real-tool tests without changing production executable defaults."""
import os
from pathlib import Path
import pytest
import douyin_recorder_app as app


@pytest.fixture(autouse=True)
def real_tools(monkeypatch):
    directory = os.environ.get("DOUYIN_TEST_TOOLS_DIR")
    if directory:
        root = Path(directory).resolve()
        for name in ("ffmpeg", "ffprobe"):
            path = root / (name + ".exe")
            if not path.is_file():
                raise RuntimeError(f"Test tool is missing: {path}")
            monkeypatch.setattr(app, f"DEFAULT_{name.upper()}_PATH", path)
        original = app._trusted_tool_roots
        monkeypatch.setattr(app, "_trusted_tool_roots", lambda: (*original(), root))
