"""Behavior checks using synthetic events/images only; no desktop capture."""

from __future__ import annotations

import json
import itertools
import queue
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from af_recorder import recorder, timeline


TARGET = {"hwnd": 101, "pid": 7, "monitor_rect": [-64, 0, 0, 48], "title": "synthetic"}


@pytest.fixture
def item(tmp_path):
    value = recorder.Recorder(TARGET, tmp_path / "recording")
    value.output_dir.mkdir()
    (value.output_dir / "screenshots").mkdir()
    value.started_ns = 1_000_000_000
    value._event_file = (value.output_dir / "events.jsonl").open("w", encoding="utf-8")
    yield value
    if not value._event_file.closed:
        value._event_file.close()


def event(kind="mouse_down", **extra):
    return {"kind": kind, "perf_ns": 2_000_000_000, "x": 20, "y": 30,
            "event_window": {"hwnd": 101, "pid": 7}, **extra}


def drain(value):
    value._stop.set()
    value._dispatch()
    value._event_file.flush()
    return timeline.read_jsonl(value.output_dir / "events.jsonl")


def cache(value, *, completed=1_900_000_000, window=None, ok=True, point=(20, 30)):
    value._cache.append({"observed_perf_ns": completed-10_000_000,
                         "completed_perf_ns": completed, "point": list(point), "ok": ok,
                         "window": window or {"hwnd": 101, "pid": 7},
                         "control": {"name": "Synthetic button", "class_name": "QButton"}})


def test_dispatch_retains_target_input_and_excludes_other_apps(item):
    item._enqueue(event())
    item._enqueue(event(event_window={"hwnd": 999, "pid": 999}))
    rows = drain(item)
    assert len(rows) == 1
    assert rows[0]["event_id"] == 1
    assert rows[0]["event_window"]["pid"] == 7


def test_dispatch_retains_owned_dialog_without_rewriting_identity(item):
    child = {"hwnd": 303, "pid": 8, "owner_chain": [{"hwnd": 101, "pid": 7}]}
    item._enqueue(event(event_window=child))
    rows = drain(item)
    assert rows[0]["event_window"] == child


def test_f9_release_does_not_create_second_mark(item):
    item._enqueue(event("key", key="F9", action="down", event_window={"pid": 999}))
    item._enqueue(event("key", key="F9", action="up", event_window={"pid": 999}))
    rows = drain(item)
    assert len([row for row in rows if row["kind"] == "mark"]) == 1


def test_f9_key_auto_repeat_is_one_mark_until_release(item):
    for action in ("down", "down", "down", "up", "down", "up"):
        item._enqueue(event("key", key="F9", action=action))
    rows = drain(item)
    assert len([row for row in rows if row["kind"] == "mark"]) == 2


def test_f10_down_and_up_request_one_stop_from_any_foreground(item, monkeypatch):
    called = threading.Event()
    calls = []
    def fake_stop():
        calls.append(1)
        called.set()
    monkeypatch.setattr(item, "stop", fake_stop)
    item._enqueue(event("key", key="F10", action="down", event_window={"pid": 999}))
    item._enqueue(event("key", key="F10", action="up", event_window={"pid": 999}))
    drain(item)
    assert called.wait(1)
    assert len(calls) == 1


def test_target_cache_uses_completed_observation_before_event(item):
    cache(item)
    cache(item, completed=2_000_000_001, point=(20, 30))
    result = item._target_cache(event())
    assert result["usable"] is True
    assert result["completed_perf_ns"] == 1_900_000_000
    assert result["control"]["name"] == "Synthetic button"


@pytest.mark.parametrize("kwargs", [{"completed": 1_000_000_000}, {"point": (100, 100)}, {"ok": False}])
def test_target_cache_rejects_stale_far_or_failed_observations(item, kwargs):
    cache(item, **kwargs)
    assert item._target_cache(event())["usable"] is False


def test_target_cache_rejects_new_modal_at_same_screen_point(item):
    cache(item)
    modal_event = event(event_window={"hwnd": 202, "pid": 7, "owner_chain": [{"hwnd": 101, "pid": 7}]})
    assert item._target_cache(modal_event)["usable"] is False


def test_missing_pre_event_observation_is_explicit(item):
    result = item._target_cache(event())
    assert result["usable"] is False
    assert result["reason"] == "no_pre_event_observation"


def test_event_queue_overflow_is_visible_as_recording_error(item):
    item._events = queue.Queue(maxsize=1)
    item._enqueue(event())
    item._enqueue(event())
    assert item.status()["dropped_events"] == 1
    assert item.status()["error"]
    assert len(item._errors) == 1


def test_before_image_must_finish_before_event(item):
    image = Image.new("RGB", (4, 4), "red")
    item._frames.append((1_700_000_000, 1_800_000_000, image))
    item._frames.append((1_950_000_000, 2_050_000_000, image))
    item._enqueue(event())
    rows = drain(item)
    before = rows[0]["screenshots"]["before"]
    assert before["completed_perf_ns"] == 1_800_000_000
    assert (item.output_dir / before["path"]).exists()


def test_before_screenshot_failure_must_not_drop_raw_input(item):
    class BrokenImage:
        def save(self, *args, **kwargs):
            raise OSError("synthetic screenshot disk failure")
    item._frames.append((1_700_000_000, 1_800_000_000, BrokenImage()))
    item._enqueue(event(button="left"))
    rows = drain(item)
    assert len(rows) == 1
    assert rows[0]["button"] == "left"
    assert item._errors


def test_after_image_capture_must_start_after_event(item, monkeypatch):
    counter = itertools.count(100, 100)
    monkeypatch.setattr(recorder, "time", SimpleNamespace(perf_counter_ns=lambda: next(counter), monotonic=lambda: 0))
    monkeypatch.setattr(recorder.ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (4, 4), "red"))
    item._stop.set()
    item._dispatch_done.set()
    item._shots.put({"event_id": 1, "event_perf_ns": 250, "due": 0})
    item._capture()
    after = json.loads((item.output_dir / "screenshots" / "000001-after.json").read_text(encoding="utf-8"))
    assert after["captured_perf_ns"] >= 250
    assert after["completed_perf_ns"] >= after["captured_perf_ns"]
    assert item._shots.empty()


def test_timeline_preserves_raw_keys_and_shot_metadata(tmp_path):
    rows = [event("mouse_down", screenshots={"after": {"path": "screenshots/a.jpg",
                                                        "captured_perf_ns": 2_500_000_000}}),
            event("mouse_move"), event("key", key="S", modifiers=["Ctrl"])]
    counts = timeline.write_timeline(tmp_path, {"valid": False}, {}, rows)
    steps = json.loads((tmp_path / "steps.json").read_text(encoding="utf-8"))
    assert counts == {"events": 3, "steps": 2}
    assert steps[0]["video_seconds"] is None
    assert steps[0]["screenshots"] == rows[0]["screenshots"]
    assert steps[1]["key"] == "S"
    assert "stage" not in steps[0]
    assert "待校准" in (tmp_path / "步骤.md").read_text(encoding="utf-8")


class FakeVideo:
    def __init__(self, *args, **kwargs):
        self.stop_calls = 0
    def start(self):
        return {"ok": True}
    def health(self):
        return {"running": True}
    def stop(self):
        self.stop_calls += 1
        return {"ok": True, "mapping": {"valid": True, "video_zero_perf_ns": 0}}


class FakeHooks:
    def __init__(self, callback):
        self.callback = callback
        self.callback_errors = self.delivery_errors = 0
        self.error = None
    def start(self):
        return self
    def stop(self):
        # A final callback queued before hook teardown must survive stop().
        self.callback(event("mouse_move", perf_ns=time.perf_counter_ns()))


@pytest.fixture
def synthetic_workers(monkeypatch):
    monkeypatch.setattr(recorder, "VideoCapture", FakeVideo)
    monkeypatch.setattr(recorder.native, "InputHooks", FakeHooks)
    monkeypatch.setattr(recorder.Recorder, "_observe", lambda self: self._stop.wait())
    monkeypatch.setattr(recorder.Recorder, "_observe_context", lambda self: self._stop.wait())
    monkeypatch.setattr(recorder.ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (64, 48), "blue"))


def test_stop_drains_queued_and_final_hook_events_once(tmp_path, synthetic_workers):
    value = recorder.Recorder(TARGET, tmp_path / "capture")
    value.start()
    for i in range(600):
        value._enqueue(event("mouse_move", perf_ns=time.perf_counter_ns(), sequence=i))
    results = []
    callers = [threading.Thread(target=lambda: results.append(value.stop())) for _ in range(2)]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(10)
    assert all(not thread.is_alive() for thread in callers)
    assert len(results) == 2
    rows = timeline.read_jsonl(value.output_dir / "events.jsonl")
    assert sorted(row["sequence"] for row in rows if "sequence" in row) == list(range(600))
    assert len(rows) == 602  # recording_started, 600 input events, final hook event
    assert value._video.stop_calls == 1
    assert value._events.empty()
    assert value._shots.empty()
    assert all(not worker.is_alive() for worker in value._threads)
    assert value.status()["dropped_events"] == 0


def test_video_start_failure_survives_in_session_manifest(tmp_path, synthetic_workers, monkeypatch):
    class BrokenVideo(FakeVideo):
        def start(self):
            raise RuntimeError("synthetic video startup failure")
    monkeypatch.setattr(recorder, "VideoCapture", BrokenVideo)
    value = recorder.Recorder(TARGET, tmp_path / "failure")
    with pytest.raises(RuntimeError, match="synthetic video startup failure"):
        value.start()
    session = json.loads((value.output_dir / "session.json").read_text(encoding="utf-8"))
    assert session["status"] == "error"
    assert any("synthetic video startup failure" in error["error"] for error in session["errors"])
    assert all(not worker.is_alive() for worker in value._threads)


def test_existing_output_is_preserved_and_start_failure_is_visible(tmp_path, synthetic_workers):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "session.json"
    sentinel.write_text("original evidence", encoding="utf-8")
    value = recorder.Recorder(TARGET, output)
    with pytest.raises(FileExistsError):
        value.start()
    assert sentinel.read_text(encoding="utf-8") == "original evidence"
    assert value.status()["state"] == "error"
    assert value.status()["error"]
