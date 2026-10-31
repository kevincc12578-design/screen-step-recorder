"""Read-only Windows observations and passive input hooks for the recorder.

No input is injected or intercepted. UIA must run inside observation_context()
on one dedicated thread. Hook callbacks do Win32 attribution and enqueue only;
UIA, screenshots and user callbacks never run in the Windows hook callback.

References:
https://learn.microsoft.com/windows/win32/winmsg/lowlevelmouseproc
https://learn.microsoft.com/windows/win32/winmsg/lowlevelkeyboardproc
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setwindowshookexw
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from contextlib import contextmanager
import queue
import threading
import time
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


def _bind(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = result
    return fn


_bind(user32, "EnumWindows", [ENUMPROC, wt.LPARAM], wt.BOOL)
_bind(user32, "IsWindow", [wt.HWND], wt.BOOL)
_bind(user32, "IsWindowVisible", [wt.HWND], wt.BOOL)
_bind(user32, "GetWindowTextLengthW", [wt.HWND], ctypes.c_int)
_bind(user32, "GetWindowTextW", [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
_bind(user32, "GetClassNameW", [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
_bind(user32, "GetWindowThreadProcessId", [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD)
_bind(user32, "GetWindowRect", [wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL)
_bind(user32, "GetWindow", [wt.HWND, wt.UINT], wt.HWND)
_bind(user32, "GetAncestor", [wt.HWND, wt.UINT], wt.HWND)
_bind(user32, "GetForegroundWindow", [], wt.HWND)
_bind(user32, "WindowFromPoint", [wt.POINT], wt.HWND)
_bind(user32, "GetCursorPos", [ctypes.POINTER(wt.POINT)], wt.BOOL)
_bind(user32, "GetDpiForWindow", [wt.HWND], wt.UINT)
_bind(user32, "MonitorFromWindow", [wt.HWND, wt.DWORD], wt.HANDLE)
_bind(user32, "GetMonitorInfoW", [wt.HANDLE, ctypes.POINTER(MONITORINFO)], wt.BOOL)
_bind(user32, "SetWindowsHookExW", [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD], wt.HANDLE)
_bind(user32, "UnhookWindowsHookEx", [wt.HANDLE], wt.BOOL)
_bind(user32, "CallNextHookEx", [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM], LRESULT)
_bind(user32, "GetMessageW", [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], wt.BOOL)
_bind(user32, "PeekMessageW", [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL)
_bind(user32, "TranslateMessage", [ctypes.POINTER(wt.MSG)], wt.BOOL)
_bind(user32, "DispatchMessageW", [ctypes.POINTER(wt.MSG)], LRESULT)
_bind(user32, "PostThreadMessageW", [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL)
_bind(user32, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short)
_bind(kernel32, "GetCurrentThreadId", [], wt.DWORD)
_bind(kernel32, "GetModuleHandleW", [wt.LPCWSTR], wt.HMODULE)
_bind(kernel32, "OpenProcess", [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
_bind(kernel32, "QueryFullProcessImageNameW", [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL)
_bind(kernel32, "CloseHandle", [wt.HANDLE], wt.BOOL)


def enable_dpi_awareness() -> dict:
    """Opt the recorder into physical screen coordinates before capture starts."""
    fn = _bind(user32, "SetProcessDpiAwarenessContext", [wt.HANDLE], wt.BOOL)
    ctypes.set_last_error(0)
    ok = bool(fn(ctypes.c_void_p(-4)))  # PER_MONITOR_AWARE_V2
    return {"requested": "PER_MONITOR_AWARE_V2", "ok": ok,
            "winerror": 0 if ok else ctypes.get_last_error()}


def _pid(hwnd: int) -> int:
    value = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
    return int(value.value)


def _title(hwnd: int) -> str:
    length = min(max(user32.GetWindowTextLengthW(hwnd), 0), 4096)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _rect(hwnd: int) -> list[int] | None:
    value = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(value)):
        return None
    return [int(value.left), int(value.top), int(value.right), int(value.bottom)]


def _process_path(pid: int) -> str | None:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _window_info(hwnd: int, *, full: bool = False) -> dict:
    hwnd = int(hwnd or 0)
    if not hwnd or not user32.IsWindow(hwnd):
        return {"hwnd": hwnd, "pid": 0, "title": "", "class_name": "", "rect": None,
                "root_hwnd": 0, "owner_chain": []}
    root = int(user32.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT
    owner_chain = []
    owner = int(user32.GetWindow(root, 4) or 0)  # GW_OWNER
    seen = {root}
    while owner and owner not in seen and len(owner_chain) < 12:
        seen.add(owner)
        owner_chain.append({"hwnd": owner, "pid": _pid(owner)})
        owner = int(user32.GetWindow(owner, 4) or 0)
    info = {"hwnd": hwnd, "pid": _pid(hwnd), "title": _title(hwnd),
            "class_name": _class(hwnd), "rect": _rect(hwnd), "root_hwnd": root,
            "root_pid": _pid(root), "owner_hwnd": owner_chain[0]["hwnd"] if owner_chain else 0,
            "owner_chain": owner_chain}
    if full:
        monitor = user32.MonitorFromWindow(hwnd, 2)  # nearest
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        valid = monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(mi))
        info.update(exe=_process_path(info["pid"]), dpi=int(user32.GetDpiForWindow(hwnd)) or None,
                    monitor_rect=[mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom] if valid else None,
                    work_rect=[mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom] if valid else None)
    return info


def enumerate_windows() -> list[dict]:
    """Visible named top-level windows; does not activate any window."""
    result = []

    @ENUMPROC
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _title(hwnd):
            result.append(_window_info(hwnd, full=True))
        return True

    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def window_at_point(x: int, y: int) -> dict:
    hit = int(user32.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    root = int(user32.GetAncestor(hit, 2) or hit) if hit else 0
    info = _window_info(root)
    info["hit_hwnd"] = hit
    return info


def foreground_window() -> dict:
    return _window_info(int(user32.GetForegroundWindow() or 0))


def cursor_position() -> tuple[int, int]:
    point = wt.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(point.x), int(point.y)


def is_target_window(info: dict | None, target: dict | int) -> bool:
    """Match the selected process or its native owned window ancestry."""
    if not info:
        return False
    target = _window_info(target) if isinstance(target, int) else target
    pid, hwnd = int(target.get("pid") or 0), int(target.get("hwnd") or 0)
    if pid and pid in (info.get("pid"), info.get("root_pid")):
        return True
    if hwnd and hwnd in (info.get("hwnd"), info.get("root_hwnd")):
        return True
    return any((pid and item.get("pid") == pid) or (hwnd and item.get("hwnd") == hwnd)
               for item in info.get("owner_chain", []))


@contextmanager
def observation_context():
    """Initialize and release UIA/COM on the thread performing observations."""
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        yield


def _control_summary(control) -> dict:
    # Never request ValuePattern/TextPattern, editable values, or password names.
    def get(name, default=None):
        try:
            return getattr(control, name)
        except Exception:
            return default

    password = bool(get("IsPassword", True))
    bounds = get("BoundingRectangle")
    rect = None
    if bounds is not None:
        rect = [int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom)]
    return {"type": get("ControlTypeName", ""), "name": "" if password else str(get("Name", "") or "")[:500],
            "class_name": str(get("ClassName", "") or ""),
            "automation_id": str(get("AutomationId", "") or ""), "rect": rect,
            "pid": int(get("ProcessId", 0) or 0), "password": password,
            "offscreen": bool(get("IsOffscreen", False))}


def observe_point(x: int, y: int) -> dict:
    """Point-in-time UIA element and at most four parents, without value reads."""
    import uiautomation as auto
    started = time.perf_counter_ns()
    try:
        control = auto.ControlFromPoint(int(x), int(y))
        if control is None:
            return {"ok": False, "error": "NO_UIA_ELEMENT", "perf_ns": started, "x": x, "y": y}
        result = _control_summary(control)
        parents = []
        current = control
        for _ in range(4):
            current = current.GetParentControl()
            if current is None:
                break
            parents.append(_control_summary(current))
        return {"ok": True, "perf_ns": started, "completed_perf_ns": time.perf_counter_ns(),
                "x": x, "y": y, "control": result, "parents": parents}
    except Exception as exc:
        return {"ok": False, "perf_ns": started, "x": x, "y": y,
                "error": type(exc).__name__, "detail": str(exc)[:250]}


def observe_context(hwnd: int, *, max_nodes: int = 500, max_depth: int = 18,
                    budget_seconds: float = 0.6) -> dict:
    """Bounded visible UIA tree. Raw classes/headings only; no stage inference.

    The budget is checked between COM calls; a stalled provider may exceed it.
    """
    import uiautomation as auto
    started = time.perf_counter_ns()
    nodes, classes, headings = [], set(), []
    truncated = False
    try:
        root = auto.ControlFromHandle(int(hwnd))
        stack = [(root, 0)] if root is not None else []
        while stack:
            if len(nodes) >= max_nodes or (time.perf_counter_ns()-started) / 1e9 > budget_seconds:
                truncated = True
                break
            control, depth = stack.pop()
            item = _control_summary(control)
            if item["offscreen"]:
                continue
            item["depth"] = depth
            nodes.append(item)
            if item["class_name"]:
                classes.add(item["class_name"])
            if item["name"] and item["type"] in {"TextControl", "TabItemControl", "CheckBoxControl", "WindowControl"}:
                headings.append({"name": item["name"], "class_name": item["class_name"], "type": item["type"]})
            if depth < max_depth and not item["password"]:
                try:
                    stack.extend((child, depth+1) for child in reversed(control.GetChildren()))
                except Exception:
                    truncated = True
            elif depth >= max_depth:
                truncated = True
        return {"ok": root is not None, "hwnd": int(hwnd), "perf_ns": started,
                "completed_perf_ns": time.perf_counter_ns(), "nodes": nodes,
                "class_names": sorted(classes), "headings": headings, "truncated": truncated,
                "limits": {"max_nodes": max_nodes, "max_depth": max_depth, "budget_seconds": budget_seconds}}
    except Exception as exc:
        return {"ok": False, "hwnd": int(hwnd), "perf_ns": started, "nodes": nodes,
                "error": type(exc).__name__, "detail": str(exc)[:250], "truncated": True}


_MODIFIER_KEYS = {0x10: "Shift", 0xA0: "Shift", 0xA1: "Shift",
                  0x11: "Ctrl", 0xA2: "Ctrl", 0xA3: "Ctrl",
                  0x12: "Alt", 0xA4: "Alt", 0xA5: "Alt", 0x5B: "Win", 0x5C: "Win"}
_NAV_KEYS = {0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Escape",
             0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
             0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
             0x2D: "Insert", 0x2E: "Delete", 0x2C: "PrintScreen", 0x13: "Pause"}


def _modifiers() -> list[str]:
    return [name for name, codes in (("Ctrl", (0x11,)), ("Alt", (0x12,)),
                                     ("Shift", (0x10,)), ("Win", (0x5B, 0x5C)))
            if any(user32.GetAsyncKeyState(code) & 0x8000 for code in codes)]


def _recordable_key(vk: int, modifiers: list[str]) -> str | None:
    if 0x70 <= vk <= 0x87:
        return f"F{vk-0x6F}"
    if vk in _NAV_KEYS:
        return _NAV_KEYS[vk]
    if vk in _MODIFIER_KEYS:
        return _MODIFIER_KEYS[vk]
    # Shift-only typing and all unmodified printable input remain unrecorded.
    if "Ctrl" in modifiers or "Alt" in modifiers:
        if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
            return chr(vk)
        return f"VK_{vk:02X}"
    return None


class InputHooks:
    """Passive low-level hooks with a message-loop thread and queue dispatcher.

    on_event receives JSON-compatible dictionaries. stop() unhooks immediately,
    wakes the message loop and drains the event queue. F9/F10 are always observed
    and never swallowed. Mouse moves are recorded only while dragging (30 Hz).
    """

    def __init__(self, on_event: Callable[[dict], None]):
        self.on_event = on_event
        self._queue = queue.SimpleQueue()
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self._dispatcher = None
        self._thread_id = 0
        self._handles = []
        self._error = None
        self._buttons = set()
        self._last_move_ns = 0
        self.callback_errors = 0
        self.delivery_errors = 0
        self._mouse_proc = HOOKPROC(self._mouse_callback)
        self._key_proc = HOOKPROC(self._key_callback)

    def start(self) -> "InputHooks":
        if self._thread and self._thread.is_alive():
            return self
        self._queue = queue.SimpleQueue()
        self._ready.clear()
        self._stopping.clear()
        self._error = None
        self._buttons.clear()
        self._thread = threading.Thread(target=self._run, name="input-hook-loop", daemon=True)
        self._dispatcher = threading.Thread(target=self._dispatch, name="input-hook-delivery", daemon=True)
        self._dispatcher.start()
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise TimeoutError("Input hook message loop did not start")
        if self._error is not None:
            self.stop()
            raise self._error
        return self

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        for handle in self._handles[:]:
            user32.UnhookWindowsHookEx(handle)
        self._handles.clear()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
        self._queue.put(None)
        if self._dispatcher and self._dispatcher is not threading.current_thread():
            self._dispatcher.join(3)

    @property
    def error(self):
        return None if self._error is None else str(self._error)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._handles and not self._stopping.is_set())

    def _dispatch(self):
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self.on_event(event)
            except Exception:
                self.delivery_errors += 1

    def _run(self):
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            message = wt.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # create queue
            module = kernel32.GetModuleHandleW(None)
            for kind, callback in ((14, self._mouse_proc), (13, self._key_proc)):
                handle = user32.SetWindowsHookExW(kind, callback, module, 0)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handles.append(handle)
            self._ready.set()
            while not self._stopping.is_set():
                status = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if status == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            for handle in self._handles[:]:
                user32.UnhookWindowsHookEx(handle)
            self._handles.clear()
            self._thread_id = 0

    def _mouse_callback(self, code, message, pointer):
        try:
            if code >= 0 and not self._stopping.is_set():
                stamp = time.perf_counter_ns()
                data = ctypes.cast(pointer, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                button_actions = {0x0201: ("left", "mouse_down"), 0x0202: ("left", "mouse_up"),
                                  0x0204: ("right", "mouse_down"), 0x0205: ("right", "mouse_up"),
                                  0x0207: ("middle", "mouse_down"), 0x0208: ("middle", "mouse_up")}
                event = None
                if message in button_actions:
                    button, kind = button_actions[message]
                    if kind == "mouse_down":
                        self._buttons.add(button)
                    else:
                        self._buttons.discard(button)
                    event = {"kind": kind, "button": button}
                elif message in (0x020B, 0x020C):
                    button = "x1" if ((data.mouseData >> 16) & 0xFFFF) == 1 else "x2"
                    kind = "mouse_down" if message == 0x020B else "mouse_up"
                    if kind == "mouse_down":
                        self._buttons.add(button)
                    else:
                        self._buttons.discard(button)
                    event = {"kind": kind, "button": button}
                elif message == 0x0200 and self._buttons and stamp-self._last_move_ns >= 33_333_333:
                    self._last_move_ns = stamp
                    event = {"kind": "mouse_move", "buttons": sorted(self._buttons)}
                elif message in (0x020A, 0x020E):
                    delta = ctypes.c_short(data.mouseData >> 16).value
                    event = {"kind": "wheel", "wheel_delta": delta,
                             "wheel_axis": "horizontal" if message == 0x020E else "vertical"}
                if event is not None:
                    x, y = int(data.pt.x), int(data.pt.y)
                    event.update(perf_ns=stamp, x=x, y=y, modifiers=_modifiers(),
                                 injected=bool(data.flags & 1), os_time_ms=int(data.time),
                                 event_window=window_at_point(x, y), foreground=foreground_window())
                    self._queue.put(event)
        except Exception:
            self.callback_errors += 1
        return user32.CallNextHookEx(None, code, message, pointer)

    def _key_callback(self, code, message, pointer):
        try:
            if code >= 0 and not self._stopping.is_set() and message in (0x0100, 0x0101, 0x0104, 0x0105):
                stamp = time.perf_counter_ns()
                data = ctypes.cast(pointer, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(data.vkCode)
                modifiers = _modifiers()
                key = _recordable_key(vk, modifiers)
                if key:
                    foreground = foreground_window()
                    x, y = cursor_position()
                    self._queue.put({"kind": "key", "perf_ns": stamp, "x": x, "y": y,
                                     "key": key, "action": "up" if message in (0x0101, 0x0105) else "down",
                                     "modifiers": modifiers, "injected": bool(data.flags & 0x10),
                                     "os_time_ms": int(data.time), "event_window": foreground,
                                     "foreground": foreground})
        except Exception:
            self.callback_errors += 1
        return user32.CallNextHookEx(None, code, message, pointer)
