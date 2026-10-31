"""Real-time FFmpeg screen capture, with auditable clock alignment.

Only this module invokes gdigrab.  Constructing an instance does not capture
anything; callers must explicitly call ``start``.  The original Matroska file
and FFmpeg logs survive a failed stop or MP4 remux.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time


DEFAULT_FFMPEG = Path(
    r"C:\Users\Administrator\.codex\tools\video-analyzer-runtime\ffmpeg"
    r"\ffmpeg-9.0.2-essentials_build\bin\ffmpeg.exe"
)
START_RE = re.compile(r"Duration:.*?\bstart:\s*(-?\d+(?:\.\d+)?)")
FIRST_FRAME_RE = re.compile(r"\bn:\s*0\s+pts:\s*(-?\d+)\s+pts_time:\s*(-?[\d.]+)")
REFERENCES = [
    "https://ffmpeg.org/ffmpeg-devices.html#gdigrab",
    "https://ffmpeg.org/ffmpeg.html#Advanced-options",
    "https://ffmpeg.org/doxygen/trunk/gdigrab_8c_source.html",
]


def _seconds_ns(value) -> int | None:
    try:
        return int(Decimal(str(value)) * 1_000_000_000)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _clock_sample() -> dict:
    before = time.perf_counter_ns()
    wall = time.time_ns()
    after = time.perf_counter_ns()
    midpoint = (before + after) // 2
    return {"wall_ns": wall, "perf_ns": midpoint,
            "pair_uncertainty_ns": (after - before + 1) // 2,
            "wall_minus_perf_ns": wall - midpoint}


def _json(path: Path, data: dict) -> None:
    # These are this capture's metadata files, reserved exclusively in start().
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _resolve_binary(explicit, name: str) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        found = shutil.which(str(explicit))
        if found:
            return Path(found).resolve()
        raise FileNotFoundError(f"{name} executable does not exist: {explicit}")
    candidate = DEFAULT_FFMPEG if name == "ffmpeg" else DEFAULT_FFMPEG.with_name("ffprobe.exe")
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    raise FileNotFoundError(f"{name} executable was not found")


class VideoCapture:
    """Capture a desktop rectangle ``(left, top, right, bottom)`` at native size.

    Desktop capture includes the mouse and popups within the chosen monitor.
    It also includes other visible windows there.  A window-only capture is
    intentionally not used, because owned modal windows must be visible.
    """

    START_TIMEOUT_S = 15.0
    STOP_TIMEOUT_S = 12.0
    TERMINATE_TIMEOUT_S = 5.0
    PROBE_TIMEOUT_S = 20.0
    REMUX_TIMEOUT_S = 60.0

    def __init__(self, output_dir, rect, fps=15, ffmpeg=None, ffprobe=None):
        self.output_dir = Path(output_dir).expanduser().resolve()
        if isinstance(rect, dict):
            rect = tuple(rect[key] for key in ("left", "top", "right", "bottom"))
        if len(rect) != 4 or any(isinstance(v, bool) or int(v) != v for v in rect):
            raise ValueError("rect must contain four integer screen coordinates")
        self.rect = tuple(int(v) for v in rect)
        if self.rect[2] <= self.rect[0] or self.rect[3] <= self.rect[1]:
            raise ValueError("rect must have positive width and height")
        if isinstance(fps, bool) or not (0 < float(fps) <= 120):
            raise ValueError("fps must be greater than zero and no more than 120")
        self.fps = float(fps)
        self._ffmpeg_arg = ffmpeg
        self._ffprobe_arg = ffprobe
        self.ffmpeg: Path | None = None
        self.ffprobe: Path | None = None
        self.process = None
        self._lock = threading.RLock()
        self._first_frame = threading.Event()
        self._threads: list[threading.Thread] = []
        self._frames = 0
        self._progress: dict = {}
        self._tail = deque(maxlen=30)
        self._clock_samples: list[dict] = []
        self._input_start_ns: int | None = None
        self._first_filter_pts_ns: int | None = None
        self._error: str | None = None
        self._reader_errors: list[str] = []
        self._started = False
        self._reserved = False
        self._start_info: dict | None = None
        self._stop_info: dict | None = None
        self._requested_stop_perf_ns: int | None = None
        self._graceful = False
        self._forced = False
        self._command: list[str] = []
        self.mkv = self.output_dir / "recording.mkv"
        self.mp4 = self.output_dir / "recording.mp4"

    def _sample(self) -> dict:
        sample = _clock_sample()
        with self._lock:
            self._clock_samples.append(sample)
        return sample

    def _build_command(self) -> list[str]:
        left, top, right, bottom = self.rect
        return [str(self.ffmpeg), "-hide_banner", "-n", "-loglevel", "info", "-nostats",
                "-stats_period", "0.25", "-progress", "pipe:1", "-copyts", "-start_at_zero",
                "-f", "gdigrab", "-framerate", f"{self.fps:g}", "-draw_mouse", "1",
                "-offset_x", str(left), "-offset_y", str(top),
                "-video_size", f"{right-left}x{bottom-top}", "-i", "desktop",
                "-map", "0:v:0", "-an", "-vf", "showinfo=checksum=0,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", "18", "-pix_fmt", "yuv420p", "-bf", "0",
                "-fps_mode", "passthrough", "-enc_time_base:v", "1:1000000",
                "-avoid_negative_ts", "disabled", "-flush_packets", "1",
                "-cluster_time_limit", "1000", "-f", "matroska", str(self.mkv)]

    def _read_stderr(self) -> None:
        try:
            with (self.output_dir / "ffmpeg.log").open("a", encoding="utf-8") as log:
                for line in self.process.stderr:
                    log.write(line)
                    log.flush()
                    with self._lock:
                        self._tail.append(line.rstrip())
                        start = START_RE.search(line)
                        if start and self._input_start_ns is None:
                            self._input_start_ns = _seconds_ns(start.group(1))
                        frame = FIRST_FRAME_RE.search(line)
                        if frame and self._first_filter_pts_ns is None:
                            self._first_filter_pts_ns = _seconds_ns(frame.group(2))
        except Exception as exc:
            with self._lock:
                self._reader_errors.append(f"stderr: {exc!r}")
                self._error = self._error or "FFMPEG_LOG_READER_FAILED"

    def _read_progress(self) -> None:
        last_sample = 0
        try:
            with (self.output_dir / "ffmpeg-progress.log").open("a", encoding="utf-8") as log:
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    key, sep, value = line.strip().partition("=")
                    if not sep:
                        continue
                    with self._lock:
                        self._progress[key] = value
                        if key == "frame":
                            try:
                                self._frames = max(self._frames, int(value))
                            except ValueError:
                                pass
                            if self._frames > 0:
                                self._first_frame.set()
                    now = time.perf_counter_ns()
                    if now - last_sample >= 1_000_000_000:
                        self._sample()
                        last_sample = now
        except Exception as exc:
            with self._lock:
                self._reader_errors.append(f"progress: {exc!r}")
                self._error = self._error or "FFMPEG_PROGRESS_READER_FAILED"

    def _mapping(self, probe: dict | None = None) -> dict:
        with self._lock:
            samples = list(self._clock_samples)
            input_start = self._input_start_ns
            first_filter = self._first_filter_pts_ns
        offsets = [s["wall_minus_perf_ns"] for s in samples]
        spread = max(offsets) - min(offsets) if offsets else None
        first_packet = _first_packet_ns(probe) if probe else None
        zero_wall = input_start + first_filter if input_start is not None and first_filter is not None else None
        zero_perf = zero_wall - offsets[0] if zero_wall is not None and offsets else None
        pts_delta = abs(first_packet - first_filter) if first_packet is not None and first_filter is not None else None
        reasons = []
        if zero_perf is None:
            reasons.append("INPUT_START_OR_FIRST_FRAME_TIMESTAMP_MISSING")
        if spread is not None and spread > 50_000_000:
            reasons.append("WALL_CLOCK_DISCONTINUITY_OR_DRIFT_OVER_50MS")
        if probe is not None and first_packet is None:
            reasons.append("FIRST_VIDEO_PACKET_PTS_MISSING")
        if pts_delta is not None and pts_delta > 2_000_000:
            reasons.append("FILTER_AND_CONTAINER_FIRST_PTS_DIFFER")
        pair_error = max((s["pair_uncertainty_ns"] for s in samples), default=0)
        uncertainty_ms = (1_000_000_000 / self.fps + (spread or 0) + pair_error + 2_000_000) / 1_000_000
        return {"valid": not reasons and probe is not None, "provisional": probe is None,
                "video_zero_perf_ns": zero_perf, "video_zero_wall_ns": zero_wall,
                "video_start_perf_ns": zero_perf, "uncertainty_ms": round(uncertainty_ms, 3),
                "uncertainty_is_estimate": True,
                "uncertainty_note": "Includes one capture interval, paired-clock spread and 2 ms timestamp allowance; unmeasured GDI/desktop latency may be larger under load. Not millisecond-precision synchronization.",
                "formula": "t_video_seconds = (event_perf_ns - video_zero_perf_ns) / 1e9; video time zero is the first captured frame",
                "pts_formula": "normalized_input_pts = gdigrab_wall_seconds - input_start_seconds; -copyts -start_at_zero preserves elapsed gaps",
                "input_start_wall_ns": input_start, "first_filter_pts_ns": first_filter,
                "first_mkv_packet_pts_ns": first_packet, "filter_packet_delta_ns": pts_delta,
                "clock_offset_spread_ns": spread, "invalid_reasons": reasons,
                "clock_samples": samples, "references": REFERENCES}

    def _base(self) -> dict:
        left, top, right, bottom = self.rect
        return {"rect": list(self.rect), "fps_requested": self.fps,
                "capture_size": [right-left, bottom-top],
                "encoded_size_expected": [(right-left+1)//2*2, (bottom-top+1)//2*2],
                "timestamp_policy": "wall-clock input PTS, passthrough, no output -r or CFR resampling",
                "audio": False, "mouse": True, "capture_source": "gdigrab desktop crop",
                "mkv": str(self.mkv), "mp4": str(self.mp4),
                "frames": self._frames, "error": self._error}

    def start(self) -> dict:
        if self._started or self._reserved:
            return {**self.health(), "ok": False, "error": "CAPTURE_ALREADY_STARTED"}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        reserved = ["command.json", "video.json", "ffmpeg.log", "ffmpeg-progress.log",
                    "recording.mkv", "recording.mp4", "ffprobe-mkv.json", "ffprobe-mp4.json", "remux.log"]
        if any((self.output_dir / name).exists() for name in reserved):
            return {"ok": False, "error": "VIDEO_OUTPUT_ALREADY_EXISTS", "output_dir": str(self.output_dir)}
        # Exclusive creation closes the race between independent recorder starts.
        try:
            with (self.output_dir / "command.json").open("x", encoding="utf-8") as file:
                file.write("{}\n")
        except FileExistsError:
            return {"ok": False, "error": "VIDEO_OUTPUT_ALREADY_EXISTS", "output_dir": str(self.output_dir)}
        self._reserved = True
        try:
            self.ffmpeg = _resolve_binary(self._ffmpeg_arg, "ffmpeg")
            self.ffprobe = _resolve_binary(self._ffprobe_arg or self.ffmpeg.with_name("ffprobe.exe"), "ffprobe")
            self._command = self._build_command()
            before = self._sample()
            _json(self.output_dir / "command.json", {"argv": self._command, "shell": False,
                  "created_clock": before, "references": REFERENCES})
            self.process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                            bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._started = True
            self._sample()
            for name, target in (("video-stderr", self._read_stderr), ("video-progress", self._read_progress)):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
                thread.start()
            deadline = time.monotonic() + self.START_TIMEOUT_S
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self._error = "FFMPEG_EXITED_BEFORE_READY"
                    break
                if self._error:
                    break
                if self._first_frame.wait(min(0.1, max(0, deadline-time.monotonic()))):
                    # A progress frame is actual encoded-frame progress, not a process-spawn acknowledgement.
                    with self._lock:
                        timestamps_ready = self._input_start_ns is not None and self._first_filter_pts_ns is not None
                    if timestamps_ready:
                        break
                    time.sleep(0.02)
            else:
                self._error = "FFMPEG_FIRST_FRAME_TIMEOUT"
            ready = (self._first_frame.is_set() and self.process.poll() is None and not self._error
                     and self._input_start_ns is not None and self._first_filter_pts_ns is not None)
            if not ready:
                self._error = self._error or "FFMPEG_START_NOT_CONFIRMED"
                self._finish_process()
            mapping = self._mapping()
            self._start_info = {**self._base(), "ok": ready, "running": ready,
                                "pid": self.process.pid, "mapping": mapping,
                                "video_start_perf_ns": mapping["video_zero_perf_ns"],
                                "start_info": {"video_start_perf_ns": mapping["video_zero_perf_ns"],
                                               "uncertainty_ms": mapping["uncertainty_ms"]}}
        except Exception as exc:
            self._error = f"VIDEO_START_FAILED: {exc!r}"
            if self.process is not None:
                self._finish_process()
            self._start_info = {**self._base(), "ok": False, "running": False, "mapping": self._mapping()}
        _json(self.output_dir / "video.json", self._start_info)
        return self._start_info

    def health(self) -> dict:
        running = self.process is not None and self.process.poll() is None
        error = self._error
        if self._started and not running and self._stop_info is None and self._requested_stop_perf_ns is None:
            error = error or "FFMPEG_EXITED_UNEXPECTEDLY"
        with self._lock:
            return {"running": running, "error": error, "frames": self._frames,
                    "exit_code": self.process.poll() if self.process is not None else None,
                    "progress": dict(self._progress)}

    def _finish_process(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.process.stdin.write("q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=self.STOP_TIMEOUT_S)
                self._graceful = self.process.returncode == 0
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self._forced = True
                self._error = self._error or "FFMPEG_STOP_TIMEOUT_OR_STDIN_FAILED"
                self.process.terminate()
                try:
                    self.process.wait(timeout=self.TERMINATE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=self.TERMINATE_TIMEOUT_S)
        for thread in self._threads:
            thread.join(timeout=3.0)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _probe(self, path: Path, label: str) -> dict:
        if not path.is_file() or not path.stat().st_size:
            return {"ok": False, "error": "VIDEO_FILE_MISSING_OR_EMPTY", "path": str(path)}
        command = [str(self.ffprobe), "-v", "error", "-select_streams", "v:0", "-show_format",
                   "-show_streams", "-show_packets", "-read_intervals", "%+#1", "-of", "json", str(path)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=self.PROBE_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(result.stdout) if result.stdout.strip() else {}
            report = {"ok": result.returncode == 0 and _first_packet_ns(data) is not None,
                      "argv": command, "exit_code": result.returncode, "stderr": result.stderr, "metadata": data}
        except Exception as exc:
            report = {"ok": False, "error": repr(exc), "argv": command}
        _json(self.output_dir / f"ffprobe-{label}.json", report)
        return report

    def _remux(self) -> dict:
        command = [str(self.ffmpeg), "-hide_banner", "-n", "-i", str(self.mkv), "-map", "0:v:0",
                   "-c", "copy", "-movflags", "+faststart", str(self.mp4)]
        try:
            with (self.output_dir / "remux.log").open("x", encoding="utf-8") as log:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        timeout=self.REMUX_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": result.returncode == 0, "exit_code": result.returncode, "argv": command,
                    "reencoded": False, "mp4": str(self.mp4)}
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "argv": command, "reencoded": False}

    def stop(self) -> dict:
        if self._stop_info is not None:
            return self._stop_info
        if not self._reserved:
            return {"ok": False, "error": "CAPTURE_NOT_STARTED", "running": False}
        self._requested_stop_perf_ns = time.perf_counter_ns()
        self._sample()
        try:
            self._finish_process()
        except Exception as exc:
            self._error = self._error or f"VIDEO_STOP_FAILED: {exc!r}"
        self._sample()
        mkv_probe = self._probe(self.mkv, "mkv") if self.ffprobe else {"ok": False, "error": "FFPROBE_UNAVAILABLE"}
        remux = {"ok": False, "skipped": True, "error": "CAPTURE_DID_NOT_END_NORMALLY"}
        mp4_probe = {"ok": False, "skipped": True}
        if self._graceful and not self._error and mkv_probe.get("ok"):
            remux = self._remux()
            if remux.get("ok"):
                mp4_probe = self._probe(self.mp4, "mp4")
        mapping = self._mapping(mkv_probe.get("metadata", {}))
        mp4_pts = _first_packet_ns(mp4_probe.get("metadata", {}))
        mapping["first_mp4_packet_pts_ns"] = mp4_pts
        mapping["playback_pts_note"] = "t_video is relative to the first captured frame; add the selected container first packet PTS when using absolute media timestamps"
        ok = bool(self._graceful and not self._error and mkv_probe.get("ok") and remux.get("ok")
                  and mp4_probe.get("ok") and mapping["valid"] and self._frames > 0)
        error = self._error or (None if ok else "VIDEO_FINALIZATION_OR_MAPPING_FAILED")
        self._stop_info = {**self._base(), "ok": ok, "error": error, "running": False,
                           "exit_code": self.process.returncode if self.process else None,
                           "graceful_stop": self._graceful, "forced_stop": self._forced,
                           "requested_stop_perf_ns": self._requested_stop_perf_ns,
                           "mapping": mapping, "video_start_perf_ns": mapping["video_zero_perf_ns"],
                           "mkv_probe": mkv_probe, "remux": remux, "mp4_probe": mp4_probe,
                           "reader_errors": self._reader_errors, "stderr_tail": list(self._tail)}
        _json(self.output_dir / "video.json", self._stop_info)
        return self._stop_info


def _first_packet_ns(data: dict) -> int | None:
    packets = data.get("packets") or []
    if not packets:
        return None
    return _seconds_ns(packets[0].get("pts_time"))
