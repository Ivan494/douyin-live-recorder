"""Public release checks, kept separate from a user's installed runtime tests."""
import json
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src"


def test_shipped_profiles_are_empty():
    assert json.loads((SOURCE / "profiles.json").read_text(encoding="utf-8-sig")) == []


def test_shipped_settings_are_safe_defaults():
    settings = json.loads((SOURCE / "settings.json").read_text(encoding="utf-8-sig"))
    assert settings.get("start_with_windows") is False
    assert settings.get("container") == "mkv"
    assert settings.get("language") == "zh-CN"
