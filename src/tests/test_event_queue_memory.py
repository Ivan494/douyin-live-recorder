"""Exercise the GUI callback/worker seam without starting Tk or loading state."""
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import douyin_recorder_app as app


def gui_for(events):
    gui = app.RecorderApp.__new__(app.RecorderApp)
    gui.queue = events
    gui.rows = {}
    gui.activity = Mock()
    gui.status_label = Mock()
    gui.root = Mock()
    gui.refresh_profiles = Mock()
    return gui


def production_events():
    # The original implementation uses Queue; the regression also measures it.
    return getattr(app, "RecorderEventQueue", queue.Queue)()


def test_refresh_exception_cannot_stop_gui_consumer():
    events = production_events()
    gui = gui_for(events)
    engine = app.MonitorEngine(SimpleNamespace(settings={}, profiles=[]), events)
    gui.refresh_profiles.side_effect = [RuntimeError("synthetic refresh failure"), None]
    engine.emit("p", "first update", status="Recording", recording=True)
    try:
        gui.process_events()
    except RuntimeError:
        pass
    assert gui.root.after.call_count == 1, "A callback failure stopped the only GUI queue drain timer"

    # Background recording continues while the next GUI callback is delayed.
    for number in range(5000):
        engine.emit("p", "recording update", elapsed=str(number))
    assert events.qsize() <= 256, "Background events retain an unbounded history while the GUI is delayed"
    gui.root.after.call_args.args[1]()
    assert gui.rows["p"] == {"status": "Recording", "recording": True, "elapsed": "4999"}
    assert gui.root.after.call_count == 2
    assert events.empty()


def test_widget_exception_reschedules_and_later_updates_are_consumed():
    events = production_events()
    gui = gui_for(events)
    gui.activity.config.side_effect = [RuntimeError("synthetic widget failure"), None]
    events.put({"profile_id": "a", "message": "first", "time": "now", "state": {"status": "Recording"}})
    try:
        gui.process_events()
    except RuntimeError:
        pass
    assert gui.root.after.call_count == 1
    events.put({"profile_id": "a", "message": "next", "time": "later", "state": {"elapsed": "1m"}})
    gui.root.after.call_args.args[1]()
    assert gui.rows["a"] == {"status": "Recording", "elapsed": "1m"}
    assert gui.root.after.call_count == 2


def test_pending_live_and_media_deltas_coalesce_without_losing_state():
    events = production_events()
    monitor = app.MonitorEngine(SimpleNamespace(settings={}, profiles=[]), events)
    with patch.object(app.MediaDownloadEngine, "_load_circuit_breaker_state"):
        media = app.MediaDownloadEngine(SimpleNamespace(settings={}, profiles=[]), events)
    monitor.emit("p", "started", status="Recording", recording=True, current_file="synthetic.mkv")
    media.emit("p", "media checked", media_status="Saved", media_progress="100%")
    for number in range(5000):
        monitor.emit("p", "elapsed update", elapsed=str(number))
        media.emit("p", "next media check", media_next_check=str(number))
    assert events.qsize() == 1
    event = events.get_nowait()
    assert event["message"] == "next media check"
    assert event["state"] == {"status": "Recording", "recording": True, "current_file": "synthetic.mkv",
                             "media_status": "Saved", "media_progress": "100%", "elapsed": "4999",
                             "media_next_check": "4999"}
    assert events.empty()


def test_pending_events_do_not_alias_mutable_producer_state():
    events = production_events()
    state = {"status": "Recording"}
    events.put({"profile_id": "p", "message": "first", "time": "now", "state": state})
    state["status"] = "mutated after put"
    assert events.get_nowait()["state"]["status"] == "Recording"


def test_distinct_producer_flood_has_fixed_capacity_and_never_blocks():
    events = production_events()
    finished = threading.Event()

    def produce():
        for number in range(5000):
            events.put({"profile_id": str(number), "message": "status", "time": "now", "state": {}})
        finished.set()

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()
    assert finished.wait(2), "Recording producer blocked on a full GUI event buffer"
    worker.join(timeout=1)
    assert events.qsize() <= 256
    pending = []
    while not events.empty():
        pending.append(events.get_nowait()["profile_id"])
    assert pending[-1] == "4999"


def test_recorder_app_constructs_bounded_queue():
    root = Mock()
    with patch.object(app, "setup_logging"), patch.object(app, "RecorderStore"), \
         patch.object(app, "MonitorEngine"), patch.object(app, "MediaDownloadEngine"), \
         patch.object(app, "Tk", return_value=root), patch.object(app, "install_exception_hooks"), \
         patch.object(app.RecorderApp, "_setup_style"), patch.object(app.RecorderApp, "_build_ui"), \
         patch.object(app.RecorderApp, "_start_tray"), patch.object(app.RecorderApp, "refresh_profiles"):
        gui = app.RecorderApp()
    assert isinstance(gui.queue, getattr(app, "RecorderEventQueue", tuple))
