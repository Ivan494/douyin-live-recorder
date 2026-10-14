"""Settings Save should not require optional YouTube tooling for Douyin users."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import douyin_recorder_app as app


def field(value):
    return SimpleNamespace(get=lambda: value)


def settings_dialog(store, ffmpeg, ytdlp):
    return SimpleNamespace(
        store=store,
        ffmpeg_var=field(str(ffmpeg)),
        ytdlp_var=field(str(ytdlp)),
        poll_var=field("60"),
        priority_risk_backoff_var=field("60"),
        standard_risk_backoff_var=field("60"),
        stall_timeout_var=field("300"),
        container_var=field("mkv"),
        hidden_var=field(False),
        autostart_var=field(False),
        keep_title_var=field(True),
        subdir_var=field(False),
        save_metadata_var=field(False),
        language_codes={"简体中文": "zh-CN"},
        language_var=field("简体中文"),
        destroy=Mock(),
    )


def save_settings(dialog, tools):
    with patch.object(app, "_trusted_tool_roots", return_value=(tools,)), patch.object(
        app, "is_autostart_enabled", return_value=False
    ), patch.object(app, "set_language"), patch.object(
        app.messagebox, "showerror"
    ) as error:
        app.SettingsDialog.save(dialog)
    return error


def test_douyin_settings_save_succeeds_without_optional_ytdlp(tmp_path):
    tools = tmp_path / "youtube-dl"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg.exe"
    ffmpeg.touch()
    missing_ytdlp = tools / "yt-dlp.exe"
    settings = app.default_settings()
    settings.update(ffmpeg_path=str(ffmpeg), ytdlp_path=str(missing_ytdlp))
    store = SimpleNamespace(settings=settings, save=Mock())
    dialog = settings_dialog(store, ffmpeg, missing_ytdlp)

    error = save_settings(dialog, tools)

    store.save.assert_called_once_with()
    dialog.destroy.assert_called_once_with()
    error.assert_not_called()
    assert Path(store.settings["ytdlp_path"]) == missing_ytdlp


def test_settings_save_still_requires_existing_ffmpeg(tmp_path):
    tools = tmp_path / "youtube-dl"
    tools.mkdir()
    missing_ffmpeg = tools / "ffmpeg.exe"
    missing_ytdlp = tools / "yt-dlp.exe"
    settings = app.default_settings()
    store = SimpleNamespace(settings=settings, save=Mock())
    dialog = settings_dialog(store, missing_ffmpeg, missing_ytdlp)

    error = save_settings(dialog, tools)

    store.save.assert_not_called()
    dialog.destroy.assert_not_called()
    error.assert_called_once()
    assert store.settings is settings


def test_settings_save_allows_empty_optional_ytdlp_path(tmp_path):
    tools = tmp_path / "youtube-dl"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg.exe"
    ffmpeg.touch()
    store = SimpleNamespace(settings=app.default_settings(), save=Mock())
    dialog = settings_dialog(store, ffmpeg, "")

    error = save_settings(dialog, tools)

    store.save.assert_called_once_with()
    dialog.destroy.assert_called_once_with()
    error.assert_not_called()
    assert store.settings["ytdlp_path"] == ""


@pytest.mark.parametrize("tool_name", ["yt-dlp.exe", "youtube-dl.exe"])
def test_settings_save_rejects_untrusted_or_wrong_ytdlp(tmp_path, tool_name):
    tools = tmp_path / "trusted"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg.exe"
    ffmpeg.touch()
    location = tmp_path / "outside" if tool_name == "yt-dlp.exe" else tools
    location.mkdir(exist_ok=True)
    ytdlp = location / tool_name
    ytdlp.touch()
    settings = app.default_settings()
    store = SimpleNamespace(settings=settings, save=Mock())
    dialog = settings_dialog(store, ffmpeg, ytdlp)

    error = save_settings(dialog, tools)

    store.save.assert_not_called()
    dialog.destroy.assert_not_called()
    error.assert_called_once()
    assert store.settings is settings


def test_settings_save_accepts_existing_trusted_ytdlp(tmp_path):
    tools = tmp_path / "youtube-dl"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg.exe"
    ffmpeg.touch()
    ytdlp = tools / "yt-dlp.exe"
    ytdlp.touch()
    store = SimpleNamespace(settings=app.default_settings(), save=Mock())
    dialog = settings_dialog(store, ffmpeg, ytdlp)

    error = save_settings(dialog, tools)

    store.save.assert_called_once_with()
    dialog.destroy.assert_called_once_with()
    error.assert_not_called()
    assert Path(store.settings["ytdlp_path"]) == ytdlp


def test_youtube_probe_still_requires_ytdlp_at_runtime(tmp_path):
    tools = tmp_path / "youtube-dl"
    tools.mkdir()
    missing_ytdlp = tools / "yt-dlp.exe"
    engine = SimpleNamespace(store=SimpleNamespace(settings={"ytdlp_path": str(missing_ytdlp)}))

    with patch.object(app, "_trusted_tool_roots", return_value=(tools,)), patch.object(
        app.subprocess, "run"
    ) as subprocess_run, pytest.raises(ValueError, match="not found"):
        app.MonitorEngine._run_ytdlp_json(engine, {}, "https://www.youtube.com/watch?v=test")

    subprocess_run.assert_not_called()
