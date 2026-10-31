"""Small Chinese desktop UI for the independent AutoForm recorder.

Tk is confined to the main thread. Window enumeration and every recorder call
run on one worker, and communicate with the UI through queues.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path, PureWindowsPath
from tkinter import messagebox, ttk
from typing import Callable

from . import native
from .recorder import Recorder


PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_DIR / "录制结果"
FPS_OPTIONS = (10, 15, 25)
SCOPE_TEXT = "录制范围：目标窗口所在整块显示器（包括弹窗和其他可见内容）"


def window_label(window: dict) -> str:
    title = str(window.get("title") or "（无标题）").replace("\n", " ").strip()
    exe = Path(str(window.get("exe") or "")).name or "未知应用"
    return f"{title}  —  {exe}  [PID {window.get('pid', '?')}, HWND {window.get('hwnd', '?')}]"


def ordered_windows(windows: list[dict]) -> list[dict]:
    """Prefer the actual R13 executable, never an application's window title."""
    def key(window):
        exe_parts = [part.casefold() for part in PureWindowsPath(str(window.get("exe") or "")).parts]
        priority = (0 if any(part in {"r13", "r13f"} for part in exe_parts) else 1) if is_autoform_window(window) else 2
        return priority, str(window.get("title") or "").casefold(), int(window.get("hwnd") or 0)

    return sorted((dict(window) for window in windows), key=key)


def is_autoform_window(window: dict) -> bool:
    return PureWindowsPath(str(window.get("exe") or "")).name.casefold() == "afformingui.exe"


def choose_target(windows: list[dict], previous: dict | None = None, *, preserve_manual: bool = False) -> dict | None:
    """Preserve explicit user choices; automatic choices must be AutoForm."""
    ordered = ordered_windows(windows)
    if preserve_manual and previous:
        match = next((window for window in ordered if (window.get("hwnd"), window.get("pid")) ==
                      (previous.get("hwnd"), previous.get("pid"))), None)
        if match is not None:
            return match
    return next((window for window in ordered if is_autoform_window(window)), None)


def fresh_target(target: dict, windows: list[dict]) -> dict:
    """Reject a closed/reused HWND and return current monitor/DPI metadata."""
    expected_exe = str(PureWindowsPath(str(target.get("exe") or ""))).casefold()
    for window in windows:
        actual_exe = str(PureWindowsPath(str(window.get("exe") or ""))).casefold()
        if ((window.get("hwnd"), window.get("pid")) == (target.get("hwnd"), target.get("pid"))
                and actual_exe == expected_exe):
            return dict(window)
    raise RuntimeError("目标窗口已关闭或发生变化，请刷新并重新选择，未开始录制。")


def format_elapsed(value: object) -> str:
    try:
        seconds = max(0, int(float(value or 0)))
    except (ValueError, TypeError, OverflowError):
        seconds = 0
    hours, remain = divmod(seconds, 3600)
    minutes, seconds = divmod(remain, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def new_session_path(root: Path = OUTPUT_ROOT) -> Path:
    # Recorder.start atomically creates the directory with exist_ok=False.
    root.mkdir(parents=True, exist_ok=True)
    while True:
        path = root / f"session-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        if not path.exists():
            return path


class RecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AutoForm 视频与步骤录制器")
        self.root.geometry("760x570")
        self.root.minsize(680, 540)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._tasks: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._windows: dict[str, dict] = {}
        self._recorder: Recorder | None = None
        self._output_dir: Path | None = None
        self._state = "idle"
        self._destroyed = False
        self._close_requested = False
        self._status_pending = False
        self._refresh_pending = False
        self._manual_selection = False
        self._last_window_refresh = 0.0
        self._countdown_id: str | None = None
        self._countdown_deadline = 0.0
        self._last_elapsed = 0.0
        self._started_monotonic = 0.0
        self._last_status_requested = 0.0
        self._status_errors = 0
        self._target = tk.StringVar()
        self._fps = tk.StringVar(value="15")
        self._note = tk.StringVar()
        self._status = tk.StringVar(value="就绪，选择目标窗口后开始录制。")
        self._elapsed = tk.StringVar(value="00:00:00")
        self._events = tk.StringVar(value="0")
        self._error = tk.StringVar(value="无")
        self._target_detail = tk.StringVar(value="未选择目标窗口")
        self._output = tk.StringVar(value=str(OUTPUT_ROOT))
        self._build_ui()
        self._worker_thread = threading.Thread(target=self._worker, name="recorder-ui-worker", daemon=True)
        self._worker_thread.start()
        self.root.after(100, self._drain_results)
        self._refresh()

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="视频与步骤同步录制", font=("Microsoft YaHei UI", 15, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(outer, text="目标窗口（AutoForm 优先，列表包含其他应用）：").grid(
            row=1, column=0, columnspan=2, sticky="w")
        self.target_combo = ttk.Combobox(outer, textvariable=self._target, state="readonly")
        self.target_combo.grid(row=2, column=0, sticky="ew", pady=(5, 6))
        self.target_combo.bind("<<ComboboxSelected>>", self._target_selected)
        target_buttons = ttk.Frame(outer)
        target_buttons.grid(row=2, column=1, padx=(8, 0), pady=(5, 6))
        self.auto_button = ttk.Button(target_buttons, text="自动选择 AutoForm R13", command=self._select_autoform)
        self.auto_button.pack(side="left")
        self.refresh_button = ttk.Button(target_buttons, text="刷新所有应用", command=self._refresh)
        self.refresh_button.pack(side="left", padx=(6, 0))
        self.target_detail_label = ttk.Label(outer, textvariable=self._target_detail, wraplength=690)
        self.target_detail_label.grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 10))
        scope = ttk.Frame(outer, padding=10, relief="solid", borderwidth=1)
        scope.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(scope, text=SCOPE_TEXT, wraplength=670, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(scope, text="录制期间请让目标应用留在这块显示器；显示器上的其他可见窗口也会被录入。",
                  wraplength=670).pack(anchor="w", pady=(4, 0))
        settings = ttk.Frame(outer)
        settings.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 12))
        ttk.Label(settings, text="视频帧率：").pack(side="left")
        self.fps_combo = ttk.Combobox(settings, textvariable=self._fps, values=FPS_OPTIONS, width=5, state="readonly")
        self.fps_combo.pack(side="left")
        ttk.Label(settings, text="fps     F9 标记步骤 · F10 停止录制").pack(side="left", padx=(5, 0))
        status_frame = ttk.Frame(outer)
        status_frame.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.status_dot = tk.Label(status_frame, text="●", fg="#65717e", font=("Segoe UI", 18))
        self.status_dot.pack(side="left", padx=(0, 6))
        ttk.Label(status_frame, textvariable=self._status, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        metrics = ttk.Frame(outer)
        metrics.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 6))
        ttk.Label(metrics, text="录制时长：").pack(side="left")
        ttk.Label(metrics, textvariable=self._elapsed, font=("Consolas", 14)).pack(side="left")
        ttk.Label(metrics, text="     已记录事件：").pack(side="left")
        ttk.Label(metrics, textvariable=self._events).pack(side="left")
        ttk.Label(outer, text="错误状态：").grid(row=8, column=0, sticky="w")
        self.error_label = ttk.Label(outer, textvariable=self._error, foreground="#a31f28", wraplength=690)
        self.error_label.grid(row=9, column=0, columnspan=2, sticky="w", pady=(2, 8))
        note_frame = ttk.Frame(outer)
        note_frame.grid(row=10, column=0, columnspan=2, sticky="ew")
        note_frame.columnconfigure(0, weight=1)
        self.note_entry = ttk.Entry(note_frame, textvariable=self._note)
        self.note_entry.grid(row=0, column=0, sticky="ew")
        self.note_entry.bind("<Return>", lambda _event: self._mark())
        self.mark_button = ttk.Button(note_frame, text="标记步骤", command=self._mark, state="disabled")
        self.mark_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(outer, text="可在上方填写步骤备注；留空也可标记。", foreground="#65717e").grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(3, 9))
        buttons = ttk.Frame(outer)
        buttons.grid(row=12, column=0, columnspan=2, sticky="ew")
        self.start_button = ttk.Button(buttons, text="开始录制", command=self._start_countdown)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消倒计时", command=self._cancel_countdown, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(buttons, text="停止并保存", command=self._stop, state="disabled")
        self.stop_button.pack(side="left")
        self.open_button = ttk.Button(buttons, text="打开结果目录", command=self._open_output)
        self.open_button.pack(side="right")
        ttk.Label(outer, textvariable=self._output, foreground="#65717e", wraplength=690).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=(11, 0))
        self._sync_buttons()

    def _worker(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            name, function = task
            try:
                self._results.put((name, function(), None))
            except Exception as exc:
                logging.exception("Recorder UI worker failed: %s", name)
                self._results.put((name, None, f"{type(exc).__name__}: {exc}"))

    def _submit(self, name: str, function: Callable) -> None:
        self._tasks.put((name, function))

    def _refresh(self, *, automatic: bool = False) -> None:
        if self._refresh_pending or self._state in {"countdown", "starting", "recording", "stopping"}:
            return
        self._refresh_pending = True
        self._last_window_refresh = time.monotonic()
        self.refresh_button.configure(state="disabled")
        self.auto_button.configure(state="disabled")
        if not automatic:
            self._status.set("正在读取可选窗口…")
        self._submit("windows", native.enumerate_windows)

    def _select_autoform(self) -> None:
        self._manual_selection = False
        self._refresh()

    def _target_selected(self, _event=None) -> None:
        self._manual_selection = True
        self._update_target_detail()
        self._sync_buttons()

    def _update_target_detail(self, _event=None) -> None:
        target = self._windows.get(self._target.get())
        if target is None:
            self._target_detail.set("未发现 AutoForm：请打开 R13 后点击自动选择，或主动选择其他应用。")
            self.target_detail_label.configure(foreground="#a31f28")
            return
        monitor = target.get("monitor_rect")
        dpi = target.get("dpi")
        exe = PureWindowsPath(str(target.get("exe") or "")).name or "未知应用"
        prefix = "AutoForm" if is_autoform_window(target) else "注意：当前目标不是 AutoForm"
        self.target_detail_label.configure(foreground="#24643b" if is_autoform_window(target) else "#a31f28")
        self._target_detail.set(f"{prefix} · {exe} · PID {target.get('pid')}\n显示器区域：{monitor}   DPI：{dpi if dpi is not None else '未提供'}")

    def _sync_buttons(self) -> None:
        idle = self._state in {"idle", "stopped", "error"} and not self._close_requested
        self.start_button.configure(state="normal" if idle and self._target.get() in self._windows and not self._refresh_pending else "disabled")
        self.refresh_button.configure(state="normal" if idle and not self._refresh_pending else "disabled")
        self.auto_button.configure(state="normal" if idle and not self._refresh_pending else "disabled")
        self.target_combo.configure(state="readonly" if idle else "disabled")
        self.fps_combo.configure(state="readonly" if idle else "disabled")
        self.cancel_button.configure(state="normal" if self._state == "countdown" else "disabled")
        self.stop_button.configure(state="normal" if self._state == "recording" else "disabled")
        self.mark_button.configure(state="normal" if self._state == "recording" else "disabled")
        self.note_entry.configure(state="normal" if self._state in {"idle", "stopped", "error", "recording"} else "disabled")
        self.status_dot.configure(fg="#d1242f" if self._state in {"recording", "stopping"} else "#65717e")

    def _start_countdown(self) -> None:
        if self._state not in {"idle", "stopped", "error"}:
            return
        if self._target.get() not in self._windows:
            self._error.set("请先选择有效目标窗口；未找到时请刷新所有应用。")
            return
        self._state = "countdown"
        self._error.set("无")
        self._countdown_deadline = time.monotonic() + 3.0
        self._sync_buttons()
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        self._countdown_id = None
        if self._state != "countdown":
            return
        remain = self._countdown_deadline - time.monotonic()
        if remain <= 0:
            self._start()
            return
        self._status.set(f"{max(1, int(remain + 0.999))} 秒后开始，可以取消…")
        self._countdown_id = self.root.after(100, self._tick_countdown)

    def _cancel_countdown(self) -> None:
        if self._countdown_id is not None:
            self.root.after_cancel(self._countdown_id)
            self._countdown_id = None
        if self._state == "countdown":
            self._state = "idle"
            self._status.set("已取消，未开始录制。")
            self._sync_buttons()

    def _start(self) -> None:
        target = dict(self._windows[self._target.get()])
        fps = int(self._fps.get())
        self._state = "starting"
        self._status.set("正在启动录制…")
        self._elapsed.set("00:00:00")
        self._events.set("0")
        self._last_elapsed = 0.0
        self._sync_buttons()

        def start_recorder():
            current_target = fresh_target(target, native.enumerate_windows())
            output = new_session_path()
            recorder = Recorder(target=current_target, output_dir=output, fps=fps)
            try:
                result = recorder.start()
                return {"recorder": recorder, "output_dir": output, "result": result}
            except Exception as exc:
                # Preserve partial files and flush the writer if startup was only
                # partly successful. The original startup error stays visible.
                try:
                    recorder.stop()
                except Exception:
                    logging.exception("Could not finalize recorder after startup failure")
                return {"recorder": recorder, "output_dir": output,
                        "result": {"ok": False, "state": "error", "error": f"{type(exc).__name__}: {exc}"}}

        self._submit("start", start_recorder)

    def _stop(self) -> None:
        if self._state != "recording" or self._recorder is None:
            return
        self._state = "stopping"
        self._status.set("正在停止并保存，请稍候…")
        self._sync_buttons()
        self._submit("stop", self._recorder.stop)

    def _mark(self) -> None:
        if self._state != "recording" or self._recorder is None:
            return
        recorder = self._recorder
        note = self._note.get().strip()
        self._note.set("")
        self._submit("mark", lambda: recorder.mark(note))

    def _open_output(self) -> None:
        path = self._output_dir or OUTPUT_ROOT
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as exc:
            self._error.set(f"无法打开目录：{exc}")

    def _close(self) -> None:
        self._close_requested = True
        if self._state == "countdown":
            self._cancel_countdown()
        if self._state == "recording":
            self._stop()
        elif self._state not in {"starting", "stopping"}:
            self._finish_close()
            return
        self._sync_buttons()

    def _finish_close(self) -> None:
        self._destroyed = True
        self._tasks.put(None)
        self.root.destroy()

    def _handle_result(self, name: str, result, error: str | None) -> None:
        if name == "status":
            self._status_pending = False
        elif name == "windows":
            self._refresh_pending = False
        if error:
            self._error.set(error)
            if name == "status":
                self._status_errors += 1
                if self._status_errors >= 3 and self._state == "recording":
                    # Losing observability must not leave a recording running
                    # silently. Normal stop still preserves any partial files.
                    self._stop()
            elif name == "stop":
                self._state = "error"
                self._status.set("停止失败，请查看错误和结果目录。")
                self._close_requested = False
            elif name == "mark":
                pass
            else:
                self._state = "error"
                self._status.set("操作失败，请查看错误。")
            self._sync_buttons()
            if self._close_requested and self._state not in {"starting", "recording", "stopping"}:
                self._finish_close()
            return

        if name == "windows":
            previous = self._windows.get(self._target.get())
            windows = ordered_windows(result or [])
            target = choose_target(windows, previous, preserve_manual=self._manual_selection)
            self._windows = {window_label(window): window for window in windows}
            labels = list(self._windows)
            self.target_combo.configure(values=labels)
            selected = window_label(target) if target else ""
            self._target.set(selected)
            self._update_target_detail()
            if self._state in {"idle", "error"}:
                self._status.set("就绪，已选中目标窗口。" if selected else "尚未选中目标；每 2 秒自动检查 AutoForm。")
        elif name == "start":
            self._recorder = result["recorder"]
            self._output_dir = Path(result["output_dir"])
            self._output.set(str(self._output_dir))
            started = result.get("result") or {}
            if started.get("ok") is False or started.get("state") in {"error", "failed"}:
                self._error.set(str(started.get("error") or "录制启动失败"))
                self._state = "recording"
                self._stop()
            else:
                self._state = "recording"
                self._started_monotonic = time.monotonic()
                self._status.set("正在录制 · F9 标记 / F10 停止")
                self._status_errors = 0
                if self._close_requested:
                    self._stop()
        elif name in {"status", "stop"}:
            self._apply_status(result or {}, is_stop=name == "stop")
        self._sync_buttons()
        if self._close_requested and self._state not in {"starting", "recording", "stopping"}:
            self._finish_close()

    def _apply_status(self, status: dict, *, is_stop: bool = False) -> None:
        self._status_errors = 0
        if "elapsed_s" in status:
            try:
                self._last_elapsed = float(status["elapsed_s"])
            except (TypeError, ValueError):
                pass
            self._elapsed.set(format_elapsed(self._last_elapsed))
        if "events" in status:
            events = status["events"]
            total = len(events) if isinstance(events, list) else events
            detail = f"{total}"
            if "target_input_events" in status:
                detail += f"（有效目标 {status['target_input_events']}，排除 {status.get('excluded_input_events', 0)}）"
            self._events.set(detail)
        if status.get("error"):
            self._error.set(str(status["error"]))
            self.error_label.configure(foreground="#a31f28")
        elif status.get("warning"):
            self._error.set(f"提示：{status['warning']}")
            self.error_label.configure(foreground="#9a5800")
        elif self._error.get().startswith("提示："):
            self._error.set("无")
            self.error_label.configure(foreground="#a31f28")
        if status.get("output_dir"):
            self._output_dir = Path(status["output_dir"])
            self._output.set(str(self._output_dir))
        state = str(status.get("state") or "").casefold()
        if state == "stopping" and self._state == "recording":
            self._state = "stopping"
            self._status.set("正在停止并保存，请稍候…")
        if is_stop or state in {"stopped", "completed", "finished", "error", "failed"}:
            # A hotkey can stop the core recorder independently of a UI action.
            self._state = "error" if state in {"error", "failed"} or status.get("ok") is False else "stopped"
            self._status.set("录制已停止，结果已保存。" if self._state == "stopped" else "录制已停止，请查看错误和结果目录。")

    def _drain_results(self) -> None:
        if self._destroyed:
            return
        while True:
            try:
                name, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._handle_result(name, result, error)
            if self._destroyed:
                return
        if self._state in {"recording", "stopping"} and self._recorder is not None:
            now = time.monotonic()
            if not self._status_pending and now - self._last_status_requested >= 0.5:
                self._last_status_requested = now
                self._status_pending = True
                self._submit("status", self._recorder.status)
        elif self._state in {"idle", "stopped", "error"} and not self._close_requested:
            if not self._refresh_pending and time.monotonic() - self._last_window_refresh >= 2.0:
                self._refresh(automatic=True)
        self.root.after(100, self._drain_results)


def main() -> None:
    dpi_status = native.enable_dpi_awareness()
    logging.info("Recorder DPI awareness: %s", dpi_status)
    root = tk.Tk()
    try:
        RecorderApp(root)
        root.mainloop()
    except Exception as exc:
        logging.exception("Recorder UI failed")
        messagebox.showerror("录制器启动失败", f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
