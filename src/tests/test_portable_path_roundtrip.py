import os
from pathlib import Path
import pytest
import douyin_recorder_app as app


@pytest.fixture
def nested_install(monkeypatch):
    pack = Path(r"E:\douyindownload\nested\Recorder")
    monkeypatch.setattr(app, "PACK_ROOT", pack)
    monkeypatch.setattr(app, "ROOT_DOWNLOAD_DIR", pack / "douyindownload")
    monkeypatch.setattr(app, "TOOLS_DIR", pack / "youtube-dl")


@pytest.mark.parametrize("token,attribute", [("${DOWNLOAD_ROOT}", "ROOT_DOWNLOAD_DIR"),
    ("%DOWNLOAD_ROOT%", "ROOT_DOWNLOAD_DIR"), ("${TOOLS_DIR}", "TOOLS_DIR"), ("${PACK_ROOT}", "PACK_ROOT")])
def test_nested_install_expands_tokens_once(nested_install, token, attribute):
    expected = str(getattr(app, attribute)) + r"\output"
    assert app.expand_portable_path(token + r"\output") == expected
    assert app.expand_portable_path(expected) == expected


def test_current_absolute_output_round_trips_without_nested_duplicate(nested_install):
    expected = str(app.ROOT_DOWNLOAD_DIR / "stories")
    assert app.expand_portable_path(app.portableize_path(expected)) == expected
    assert app.expand_portable_path(expected) == expected


def test_legacy_root_migration_still_works(nested_install):
    suffix = r"\saved" if os.name == "nt" else "/saved"
    assert app.expand_portable_path(r"E:\douyindownload\saved") == str(app.ROOT_DOWNLOAD_DIR) + suffix
    assert app.expand_portable_path("E:/youtube-dl/ffmpeg.exe") == str(app.TOOLS_DIR) + "/ffmpeg.exe"


@pytest.mark.parametrize("legacy,attribute", [
    (r"E:\douyindownload", "ROOT_DOWNLOAD_DIR"), (r"E:\youtube-dl", "TOOLS_DIR"),
])
def test_legacy_backslash_suffix_uses_platform_separators(monkeypatch, tmp_path, legacy, attribute):
    for name, directory in (("PACK_ROOT", "install"), ("ROOT_DOWNLOAD_DIR", "downloads"), ("TOOLS_DIR", "tools")):
        monkeypatch.setattr(app, name, tmp_path / directory)
    suffix = r"\saved\file" if os.name == "nt" else "/saved/file"
    expected = str(getattr(app, attribute)) + suffix
    assert app.expand_portable_path(legacy + r"\saved\file") == expected
    assert app.expand_portable_path(expected) == expected


def test_sibling_folder_prefix_is_not_a_root(nested_install):
    value = str(app.ROOT_DOWNLOAD_DIR) + r"-other\saved"
    assert app.portableize_path(value) != "${DOWNLOAD_ROOT}" + r"-other\saved"
    assert app.expand_portable_path(r"E:\douyindownload-other\saved") == r"E:\douyindownload-other\saved"
