"""Launch the recorder UI or check dependencies without recording."""
import argparse
import importlib.metadata
import json
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="本项目独立视频与步骤录制器")
    parser.add_argument("--doctor", action="store_true", help="只检查依赖，不录屏")
    args = parser.parse_args()
    if not args.doctor:
        from .gui import main as gui_main
        return gui_main()
    import tkinter
    from .video import _resolve_binary
    from . import native
    report = {"python": sys.executable, "tk": tkinter.TkVersion,
              "pillow": importlib.metadata.version("Pillow"),
              "uiautomation": importlib.metadata.version("uiautomation"), "ok": True}
    for name in ["ffmpeg", "ffprobe"]:
        try:
            binary = _resolve_binary(None, name)
            run = subprocess.run([str(binary), "-version"], capture_output=True, text=True,
                                 timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report[name] = {"path": str(binary), "version": run.stdout.splitlines()[0], "ok": run.returncode == 0}
            report["ok"] &= run.returncode == 0
        except Exception as exc:
            report[name] = {"ok": False, "error": str(exc)}
            report["ok"] = False
    report["visible_windows"] = len(native.enumerate_windows())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
