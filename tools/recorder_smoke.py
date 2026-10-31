"""Visible synthetic-window smoke test. Does not open or control AutoForm."""
from __future__ import annotations
import json
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from af_recorder import native
native.enable_dpi_awareness()
from af_recorder.recorder import Recorder

out = Path(__file__).resolve().parents[1] / "录制器自检" / f"smoke-{datetime.now():%Y%m%d-%H%M%S}"
root = tk.Tk()
root.title("Recorder Synthetic Test")
root.attributes("-fullscreen", True)
root.attributes("-topmost", True)
root.configure(bg="#eff4f5")
status = tk.StringVar(value="正在启动同步录制自检…")
count = tk.IntVar(value=0)
tk.Label(root, text="录制器独立测试窗口", bg="#eff4f5", font=("Microsoft YaHei UI", 30)).pack(pady=60)
tk.Label(root, text="测试只作用于此窗口，不操作 AutoForm。", bg="#eff4f5", font=("Microsoft YaHei UI", 15)).pack()
tk.Label(root, textvariable=status, bg="#eff4f5", font=("Microsoft YaHei UI", 18)).pack(pady=20)
tk.Button(root, text="单击计数", command=lambda: count.set(count.get()+1), font=("Microsoft YaHei UI", 18), width=20).pack(pady=10)
tk.Label(root, textvariable=count, bg="#eff4f5", font=("Consolas", 20)).pack()
def modal():
    dialog=tk.Toplevel(root)
    dialog.title("Recorder Test Dialog")
    dialog.geometry("500x180+700+480")
    dialog.attributes("-topmost",True)
    dialog.transient(root)
    tk.Label(dialog,text="弹窗事件归属测试",font=("Microsoft YaHei UI",18)).pack(pady=30)
    tk.Button(dialog,text="确定",command=dialog.destroy,font=("Microsoft YaHei UI",16)).pack()
tk.Button(root,text="打开测试弹窗",command=modal,font=("Microsoft YaHei UI",16),width=20).pack(pady=10)
tk.Label(root,text="F9 标记 · F10 停止并结束自检",bg="#eff4f5",font=("Microsoft YaHei UI",14)).pack(pady=20)
recorder = None
results = queue.Queue()
ui_events = []
def log(event):
    ui_events.append({"perf_ns":time.perf_counter_ns(),"event":str(event.type),"x":event.x_root,"y":event.y_root,"keysym":event.keysym})
root.bind_all("<ButtonPress>",log,add=True)
root.bind_all("<ButtonRelease>",log,add=True)
root.bind_all("<KeyPress>",log,add=True)
def start():
    global recorder
    target=next(w for w in native.enumerate_windows() if w["pid"]==os.getpid() and w["title"]=="Recorder Synthetic Test")
    recorder=Recorder(target,out)
    def work():
        try: results.put(recorder.start())
        except Exception as exc: results.put({"error":repr(exc)})
    threading.Thread(target=work,daemon=True).start()
def poll():
    while not results.empty():
        value=results.get()
        status.set("正在录制：可以点击按钮、打开弹窗并用 F9/F10 测试。" if value.get("ok") else str(value))
    if recorder and recorder.state == "recording":
        current=recorder.status()
        status.set(f"正在录制 {current['elapsed_s']:.1f}s · 事件 {current['events']} · 控件观察 {current['point_observations']}")
    if recorder and recorder.state in {"stopped","error"}:
        payload={"recorder":recorder.status(),"button_click_count":count.get(),"ui_events":ui_events}
        (out/"selftest.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(payload,ensure_ascii=False),flush=True)
        root.destroy()
        return
    root.after(150,poll)
def stop():
    if recorder:
        threading.Thread(target=recorder.stop,daemon=True).start()
    else: root.destroy()
root.protocol("WM_DELETE_WINDOW",stop)
root.after(500,start)
root.after(150,poll)
root.after(60000,stop)
root.mainloop()
