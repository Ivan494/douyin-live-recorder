"""Exercise the menu callback after a profile folder is moved in Explorer."""
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

import douyin_recorder_app as app


@pytest.fixture
def menu(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(app, "ROOT_DOWNLOAD_DIR", tmp_path / "downloads")
    store = app.RecorderStore.__new__(app.RecorderStore)
    store.lock = threading.RLock()
    store.settings = app.default_settings()
    store.profiles = [{"id": "fixture", "name": "Fixture", "url": "https://live.douyin.com/123",
                       "output_dir": str(tmp_path / "old folder")}]
    ui = app.RecorderApp.__new__(app.RecorderApp)
    ui.root = Mock()
    ui.store = store
    ui.engine = Mock()
    ui.media_engine = Mock()
    ui.refresh_profiles = Mock()
    ui.selected_profile = lambda: store.get_profile("fixture")
    monkeypatch.setattr(app.messagebox, "showerror", Mock())
    monkeypatch.setattr(app.filedialog, "askdirectory", Mock(return_value=""))
    def open_existing(path):
        if not Path(path).is_dir():
            raise FileNotFoundError(path)
    monkeypatch.setattr(app.os, "startfile", Mock(side_effect=open_existing))
    return ui


def test_moved_profile_folder_is_selected_saved_and_opened(menu, tmp_path):
    old = Path(menu.selected_profile()["output_dir"])
    old.mkdir()
    (old / "saved.mp4").write_bytes(b"existing media")
    new = tmp_path / "新位置 with spaces"
    old.rename(new)
    cancellation = app.ProfileCancellation(threading.Event(), menu.store, menu.selected_profile())
    app.filedialog.askdirectory.return_value = str(new)

    menu.open_folder()

    assert menu.selected_profile()["output_dir"] == str(new)
    assert app.load_json(app.PROFILES_FILE, [])[0]["output_dir"] == str(new)
    app.os.startfile.assert_called_once_with(str(new))
    assert (new / "saved.mp4").read_bytes() == b"existing media"
    assert not old.exists()
    assert cancellation.is_set()  # Work targeting the former folder must stop.
    menu.engine.profile_changed.assert_called_once()
    menu.media_engine.refresh_profile.assert_called_once_with("fixture")
    app.messagebox.showerror.assert_not_called()


def test_cancel_keeps_missing_folder_and_profile_unchanged(menu):
    original = dict(menu.selected_profile())
    menu.open_folder()
    assert menu.selected_profile() == original
    assert not Path(original["output_dir"]).exists()
    assert not app.PROFILES_FILE.exists()
    app.os.startfile.assert_not_called()


def test_existing_folder_opens_without_relocation(menu):
    path = Path(menu.selected_profile()["output_dir"])
    path.mkdir()
    menu.open_folder()
    app.os.startfile.assert_called_once_with(str(path))
    app.filedialog.askdirectory.assert_not_called()


def test_open_failure_is_reported_instead_of_uncaught_callback(menu):
    Path(menu.selected_profile()["output_dir"]).mkdir()
    app.os.startfile.side_effect = PermissionError("Access denied")
    menu.open_folder()
    app.messagebox.showerror.assert_called_once()


def test_failed_save_preserves_previous_profile(menu, tmp_path, monkeypatch):
    original = dict(menu.selected_profile())
    app.filedialog.askdirectory.return_value = str(tmp_path)
    monkeypatch.setattr(app, "save_json", Mock(side_effect=OSError("Disk full")))
    menu.open_folder()
    assert menu.selected_profile() == original
    app.messagebox.showerror.assert_called_once()
    app.os.startfile.assert_not_called()
    menu.engine.profile_changed.assert_not_called()


def test_tray_creates_missing_root_folder(menu):
    menu.open_root_folder()
    assert app.ROOT_DOWNLOAD_DIR.is_dir()
    app.os.startfile.assert_called_once_with(str(app.ROOT_DOWNLOAD_DIR))


def test_profile_deleted_during_folder_selection_is_not_restored(menu, tmp_path):
    def select_after_delete(**kwargs):
        menu.store.profiles = []
        return str(tmp_path)
    app.filedialog.askdirectory.side_effect = select_after_delete
    menu.open_folder()
    assert not menu.store.profiles
    app.os.startfile.assert_not_called()
    assert not app.PROFILES_FILE.exists()


def test_invalid_replacement_does_not_change_profile(menu, tmp_path):
    original = dict(menu.selected_profile())
    app.filedialog.askdirectory.return_value = str(tmp_path / "also missing")
    menu.open_folder()
    assert menu.selected_profile() == original
    app.messagebox.showerror.assert_called_once()
    app.os.startfile.assert_not_called()
