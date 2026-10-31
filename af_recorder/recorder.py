"""Coordinate video, passive input hooks and independent observation threads."""
from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PIL import ImageGrab

from . import native
from .timeline import write_timeline
from .video import VideoCapture


class Recorder:
    def __init__(self, target: dict, output_dir: Path, fps: int = 15):
        self.target = dict(target)
        self.output_dir = Path(output_dir).resolve()
        self.fps = fps
        self.state = "idle"
        self.error = None
        self.started_ns = None
        self.ended_ns = None
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stop = threading.Event()
        self._events = queue.Queue(maxsize=20000)
        self._shots = queue.Queue(maxsize=1000)
        self._cache = deque(maxlen=12)
        self._frames = deque(maxlen=3)
        self._count = 0
        self._dropped = 0
        self._errors = []
        self._threads = []
        self._hooks = None
        self._video = None
        self._event_file = None
        self._observation_file = None
        self._stop_requested = False
        self._dispatch_done = threading.Event()
        self._owns_dir = False
        self._hotkeys_down = set()
        self._point_observations = 0
        self._observation_lock = threading.Lock()
        self._input_count = 0
        self._excluded_count = 0

    def _fail(self, where: str, error: object) -> None:
        entry = {"where": where, "error": str(error), "perf_ns": time.perf_counter_ns()}
        with self._lock:
            self._errors.append(entry)
            self.error = f"{where}: {error}"

    def status(self) -> dict:
        with self._lock:
            end = self.ended_ns or time.perf_counter_ns()
            elapsed = (end - self.started_ns) / 1e9 if self.started_ns else 0.0
            return {"state": self.state, "elapsed_s": max(0.0, elapsed), "events": self._count,
                    "error": self.error, "dropped_events": self._dropped,
                    "target_input_events": self._input_count, "excluded_input_events": self._excluded_count,
                    "warning": "尚未记录到目标窗口操作，请核对目标后再演示。" if self.state == "recording" and elapsed > 15 and not self._input_count else None,
                    "point_observations": self._point_observations, "output_dir": str(self.output_dir)}

    def _enqueue(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("perf_ns", time.perf_counter_ns())
        try:
            self._events.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            self._fail("event_queue", "事件队列已满，录制证据不完整")

    def mark(self, note: str = "") -> None:
        if self.state == "recording":
            self._enqueue({"kind": "mark", "note": note or "用户标记"})

    def start(self) -> dict:
        with self._lock:
            if self.state != "idle":
                raise RuntimeError("recorder already started")
            self.state = "starting"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=False)
            self._owns_dir = True
            (self.output_dir / "screenshots").mkdir()
            self._event_file = (self.output_dir / "events.jsonl").open("a", encoding="utf-8", buffering=1)
            self._observation_file = (self.output_dir / "observations.jsonl").open("a", encoding="utf-8", buffering=1)
            self.started_ns = time.perf_counter_ns()
            self._write_session({"status": "starting"})
            rect = self.target.get("monitor_rect")
            if not rect or len(rect) != 4:
                raise ValueError("目标窗口缺少显示器范围")
            self._video = VideoCapture(self.output_dir, rect, fps=self.fps)
            started = self._video.start()
            if started.get("ok") is False:
                raise RuntimeError(started.get("error", "video startup failed"))
            for name, fn in [("observations", self._observe), ("context", self._observe_context),
                             ("screenshots", self._capture), ("events", self._dispatch)]:
                thread = threading.Thread(target=fn, daemon=True, name=f"recorder-{name}")
                self._threads.append(thread)
                thread.start()
            self._hooks = native.InputHooks(self._enqueue)
            self._hooks.start()
            with self._lock:
                self.state = "recording"
            self._enqueue({"kind": "recording_started", "target": self.target})
            self._write_session({"status": "recording", "video_start": started})
            return {"ok": True, **self.status()}
        except Exception as exc:
            self._fail("start", exc)
            if self._owns_dir:
                try:
                    self.stop()
                except Exception as stop_exc:
                    self._fail("startup_cleanup", stop_exc)
                    self.state = "error"
            else:
                self.state = "error"
            raise

    def _write_session(self, extra: dict) -> None:
        value = {"schema_version": 1, "created_at": datetime.now().astimezone().isoformat(),
                 "target": self.target, "capture_scope": "target_monitor_desktop", "fps": self.fps,
                 "clock": "time.perf_counter_ns", "started_perf_ns": self.started_ns,
                 "keyboard_policy": "shortcuts_and_navigation_only_no_plain_text", "audio": "none",
                 "stage_inference": False, "errors": list(self._errors), **self.status(), **extra}
        path = self.output_dir / "session.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _observe(self) -> None:
        try:
            with native.observation_context():
                while not self._stop.is_set():
                    started = time.perf_counter_ns()
                    x, y = native.cursor_position()
                    window = native.window_at_point(x, y)
                    if native.is_target_window(window, self.target):
                        observation = native.observe_point(x, y)
                        cached = {"observed_perf_ns": started, "completed_perf_ns": time.perf_counter_ns(),
                                  "point": [x, y], "ok": observation.get("ok", False),
                                  "control": observation.get("control", {}),
                                  "parents": observation.get("parents", []),
                                  "error": observation.get("error"), "window": window}
                        with self._lock:
                            self._cache.append(cached)
                            self._point_observations += 1
                        self._write_observation({"kind": "pointer", **cached})
                    self._stop.wait(0.15)
        except Exception as exc:
            self._fail("uia_observation", exc)

    def _write_observation(self, entry: dict) -> None:
        with self._observation_lock:
            if self._observation_file and not self._observation_file.closed:
                self._observation_file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _observe_context(self) -> None:
        try:
            with native.observation_context():
                previous_hwnd = None
                while not self._stop.is_set():
                    started = time.perf_counter_ns()
                    foreground = native.foreground_window()
                    matched = native.is_target_window(foreground, self.target)
                    hwnd = foreground.get("hwnd") if matched else self.target["hwnd"]
                    if matched and hwnd != previous_hwnd:
                        self._enqueue({"kind": "focus", "perf_ns": started, "event_window": foreground})
                    previous_hwnd = hwnd
                    context = native.observe_context(hwnd)
                    entry = {"perf_ns": started, "completed_perf_ns": time.perf_counter_ns(),
                             "foreground": foreground, "context": context}
                    self._write_observation({"kind": "context", **entry})
                    self._stop.wait(1.0)
        except Exception as exc:
            self._fail("uia_context", exc)

    def _target_cache(self, event: dict) -> dict:
        with self._lock:
            cached = copy.deepcopy(next((c for c in reversed(self._cache)
                                         if c["completed_perf_ns"] <= event["perf_ns"]), {}))
        if not cached:
            return {"usable": False, "reason": "no_pre_event_observation"}
        age = (event["perf_ns"] - cached["completed_perf_ns"]) / 1e6
        observed_window = cached.get("window") or {}
        event_window = event.get("event_window") or event.get("window") or {}
        observed_hwnd = observed_window.get("root_hwnd") or observed_window.get("hwnd")
        event_hwnd = event_window.get("root_hwnd") or event_window.get("hwnd")
        same_window = bool(observed_hwnd and observed_hwnd == event_hwnd)
        pointer = event.get("x"), event.get("y")
        distance = math.dist(pointer, cached["point"]) if all(v is not None for v in pointer) else None
        cached.update(age_ms=round(age, 3), pointer_distance_px=distance,
                      usable=bool(same_window and cached.get("ok") and 0 <= age <= 600 and distance is not None and distance <= 16))
        if not same_window:
            cached["reason"] = "window_changed"
        return cached

    def _dispatch(self) -> None:
        while not self._stop.is_set() or not self._events.empty():
            try:
                event = self._events.get(timeout=0.15)
            except queue.Empty:
                continue
            try:
                key = str(event.get("key", "")).upper()
                hotkey_down = event.get("kind") == "key" and event.get("action", "down") == "down" and key not in self._hotkeys_down
                if event.get("kind") == "key":
                    if event.get("action") == "up":
                        self._hotkeys_down.discard(key)
                    else:
                        self._hotkeys_down.add(key)
                if hotkey_down and key in {"F9", "F10"}:
                    if key == "F9":
                        event.update(kind="mark", note="F9 用户标记")
                    elif not self._stop_requested:
                        self._stop_requested = True
                        threading.Thread(target=self.stop, daemon=True, name="recorder-hotkey-stop").start()
                info = event.get("event_window") or event.get("window") or {}
                exempt = event.get("kind") in {"mark", "recording_started", "error"} or key == "F10"
                if not exempt and not native.is_target_window(info, self.target):
                    self._excluded_count += 1
                    continue
                if event.get("kind") in {"mouse_down", "mouse_up", "mouse_move", "wheel", "key"} and native.is_target_window(info, self.target):
                    self._input_count += 1
                self._count += 1
                event["event_id"] = self._count
                event["elapsed_s"] = round((event["perf_ns"] - self.started_ns) / 1e9, 6)
                if event.get("kind") in {"mouse_down", "mouse_up", "wheel"}:
                    event["target_before"] = self._target_cache(event)
                event["screenshots"] = {}
                if event.get("kind") != "mouse_move":
                    with self._lock:
                        previous = next((f for f in reversed(self._frames) if f[1] <= event["perf_ns"]), None)
                    if previous:
                        captured, completed, image = previous
                        path = f"screenshots/{self._count:06}-before.jpg"
                        try:
                            image.save(self.output_dir / path, quality=88)
                            event["screenshots"]["before"] = {"path": path, "captured_perf_ns": captured,
                                "completed_perf_ns": completed, "age_ms": round((event["perf_ns"] - completed) / 1e6, 3)}
                        except Exception as exc:
                            event["screenshots"]["before"] = {"error": str(exc)}
                            self._fail("before_screenshot", exc)
                    try:
                        self._shots.put_nowait({"event_id": self._count, "event_perf_ns": event["perf_ns"],
                                               "due": time.perf_counter_ns() + 350_000_000})
                    except queue.Full:
                        self._fail("screenshot_queue", "截图队列已满")
                self._event_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception as exc:
                self._fail("dispatch", exc)
        self._dispatch_done.set()

    def _capture(self) -> None:
        pending = []
        last_health = 0.0
        try:
            while not self._stop.is_set() or not self._dispatch_done.is_set() or pending or not self._shots.empty():
                captured = time.perf_counter_ns()
                image = ImageGrab.grab(bbox=tuple(self.target["monitor_rect"]), all_screens=True)
                completed = time.perf_counter_ns()
                with self._lock:
                    self._frames.append((captured, completed, image))
                while True:
                    try:
                        pending.append(self._shots.get_nowait())
                    except queue.Empty:
                        break
                for job in pending[:]:
                    if captured >= job["event_perf_ns"] and (time.perf_counter_ns() >= job["due"] or self._stop.is_set()):
                        path = f"screenshots/{job['event_id']:06}-after.jpg"
                        image.save(self.output_dir / path, quality=88)
                        sidecar = {"event_id": job["event_id"], "path": path, "captured_perf_ns": captured,
                                   "completed_perf_ns": completed,
                                   "delay_ms": round((captured-job["event_perf_ns"])/1e6, 3)}
                        (self.output_dir / "screenshots" / f"{job['event_id']:06}-after.json").write_text(
                            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
                        pending.remove(job)
                if time.monotonic() - last_health > 1:
                    health = self._video.health()
                    if health.get("error") or health.get("running") is False:
                        self._fail("video_health", health.get("error") or "视频采集已退出")
                        if not self._stop_requested:
                            self._stop_requested = True
                            threading.Thread(target=self.stop, daemon=True).start()
                    last_health = time.monotonic()
                self._stop.wait(0.25)
        except Exception as exc:
            self._fail("screenshots", exc)

    def stop(self) -> dict:
        with self._stop_lock:
            if self.state in {"stopped", "error"}:
                return {"ok": self.state == "stopped", **self.status()}
            self.state = "stopping"
            if self._hooks:
                try:
                    self._hooks.stop()
                except Exception as exc:
                    self._fail("hooks_stop", exc)
                if self._hooks.error or self._hooks.callback_errors or self._hooks.delivery_errors:
                    self._fail("input_hooks", {"error": self._hooks.error,
                        "callback_errors": self._hooks.callback_errors, "delivery_errors": self._hooks.delivery_errors})
            self._stop.set()
            for thread in sorted(self._threads, key=lambda t: 0 if t.name == "recorder-events" else 1):
                if thread is not threading.current_thread():
                    thread.join(timeout=4)
                    if thread.is_alive():
                        self._fail("worker_stop", f"{thread.name} 未及时退出")
            video = {}
            if self._video:
                try:
                    video = self._video.stop()
                    if video.get("ok") is False:
                        self._fail("video_stop", video.get("error", "video failed"))
                except Exception as exc:
                    self._fail("video_stop", exc)
            self.ended_ns = time.perf_counter_ns()
            for f in [self._event_file, self._observation_file]:
                if f:
                    f.flush()
                    f.close()
            mapping = video.get("mapping") or {}
            if not mapping.get("valid"):
                self._fail("video_alignment", "未建立有效的视频时间映射，保留原始时间等待校准")
            counts = {}
            if self.output_dir.exists():
                try:
                    # Attach independently timestamped after shots; raw events remain untouched.
                    from .timeline import read_jsonl
                    events = read_jsonl(self.output_dir / "events.jsonl")
                    for event in events:
                        shot = self.output_dir / "screenshots" / f"{event['event_id']:06}-after.json"
                        if shot.exists():
                            event.setdefault("screenshots", {})["after"] = json.loads(shot.read_text(encoding="utf-8"))
                    counts = write_timeline(self.output_dir, mapping, video, events)
                except Exception as exc:
                    self._fail("timeline", exc)
            self.state = "error" if self._errors or self._dropped else "stopped"
            if self._owns_dir:
                try:
                    self._write_session({"status": self.state, "video": video, "mapping": mapping, **counts})
                except Exception as exc:
                    self._fail("session_manifest", exc)
                    self.state = "error"
            return {"ok": self.state == "stopped", **self.status(), "video": video}
