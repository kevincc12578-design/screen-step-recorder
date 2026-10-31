"""Offline tests: fake FFmpeg processes and synthetic metadata, never a desktop."""

import io
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from af_recorder import video


def capture(tmp_path, **kwargs):
    return video.VideoCapture(tmp_path, (-1920, 0, -1, 1079), ffmpeg="ffmpeg", ffprobe="ffprobe", **kwargs)


def clock_sample(wall=1_700_000_000_000_000_000, perf=2_000_000_000):
    return {"wall_ns": wall, "perf_ns": perf, "pair_uncertainty_ns": 100,
            "wall_minus_perf_ns": wall-perf}


class FakeInput(io.StringIO):
    def __init__(self, proc):
        super().__init__()
        self.proc = proc

    def write(self, text):
        result = super().write(text)
        if "q" in text and not self.proc.ignore_q:
            self.proc.returncode = 0
        return result


class FakeProcess:
    def __init__(self, command, *, immediate_exit=None, ignore_q=False, emit_frame=True, **kwargs):
        self.command = command
        self.pid = 77
        self.returncode = immediate_exit
        self.ignore_q = ignore_q
        self.terminated = False
        self.killed = False
        self.stdin = FakeInput(self)
        log = "Input #0, gdigrab, from 'desktop':\n  Duration: N/A, start: 1700000000.000000, bitrate: 1 kb/s\n"
        if emit_frame:
            log += "[Parsed_showinfo_0 @ 00000] n:   0 pts:      0 pts_time:0 duration:66667\n"
        self.stderr = io.StringIO(log)
        self.stdout = io.StringIO("frame=1\nout_time_us=66667\nprogress=continue\n" if emit_frame else "")
        Path(command[-1]).write_bytes(b"fake-matroska")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_binaries(monkeypatch):
    monkeypatch.setattr(video, "_resolve_binary", lambda explicit, name: Path(f"C:/{name}.exe"))
    monkeypatch.setattr(video, "_clock_sample", clock_sample)


def test_command_preserves_elapsed_timestamps_and_original_size(tmp_path):
    item = capture(tmp_path)
    item.ffmpeg = Path("ffmpeg.exe")
    command = item._build_command()
    assert command[command.index("-offset_x")+1] == "-1920"
    assert command[command.index("-video_size")+1] == "1919x1079"
    assert command[command.index("-framerate")+1] == "15"
    assert command[command.index("-fps_mode")+1] == "passthrough"
    assert command[command.index("-enc_time_base:v")+1] == "1:1000000"
    assert "-copyts" in command and "-start_at_zero" in command
    assert "-r" not in command and "-y" not in command
    assert command[command.index("-i")+1] == "desktop"
    assert command[command.index("-draw_mouse")+1] == "1"
    assert "pad=ceil(iw/2)*2:ceil(ih/2)*2" in command[command.index("-vf")+1]
    assert item._base()["encoded_size_expected"] == [1920, 1080]


@pytest.mark.parametrize("rect,fps", [((0,0,0,4),15), ((4,0,0,4),15), ((0,0,4,4),0), ((0,0,4,4),121)])
def test_rejects_invalid_geometry_or_sampling(tmp_path, rect, fps):
    with pytest.raises(ValueError):
        video.VideoCapture(tmp_path, rect, fps)


def test_start_waits_for_frame_and_exposes_provisional_clock(tmp_path, monkeypatch, fake_binaries):
    monkeypatch.setattr(video.subprocess, "Popen", FakeProcess)
    item = capture(tmp_path)
    result = item.start()
    assert result["ok"] and result["running"]
    assert result["video_start_perf_ns"] == 2_000_000_000
    assert result["mapping"]["valid"] is False
    assert result["mapping"]["provisional"] is True
    assert item.health()["frames"] == 1
    assert item.start()["error"] == "CAPTURE_ALREADY_STARTED"
    item._finish_process()
    command = json.loads((tmp_path / "command.json").read_text(encoding="utf-8"))
    assert command["shell"] is False
    assert (tmp_path / "ffmpeg.log").read_text(encoding="utf-8").find("1700000000") >= 0


def test_start_timeout_terminates_owned_process_and_keeps_mkv(tmp_path, monkeypatch, fake_binaries):
    factory = lambda command, **kwargs: FakeProcess(command, ignore_q=True, emit_frame=False, **kwargs)
    monkeypatch.setattr(video.subprocess, "Popen", factory)
    item = capture(tmp_path)
    item.START_TIMEOUT_S = 0.02
    item.STOP_TIMEOUT_S = 0.01
    result = item.start()
    assert result["ok"] is False
    assert result["error"] == "FFMPEG_FIRST_FRAME_TIMEOUT"
    assert item.process.terminated
    assert item.mkv.is_file()
    assert not item.mp4.exists()


def test_existing_capture_output_refused_without_process(tmp_path, monkeypatch):
    previous = tmp_path / "recording.mkv"
    previous.write_bytes(b"previous-evidence")
    start = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr(video.subprocess, "Popen", start)
    assert capture(tmp_path).start()["error"] == "VIDEO_OUTPUT_ALREADY_EXISTS"
    assert previous.read_bytes() == b"previous-evidence"
    start.assert_not_called()


def test_failed_spawn_writes_error_evidence(tmp_path, monkeypatch, fake_binaries):
    monkeypatch.setattr(video.subprocess, "Popen", Mock(side_effect=OSError("launch failed")))
    result = capture(tmp_path).start()
    assert result["ok"] is False
    assert "launch failed" in result["error"]
    assert json.loads((tmp_path / "video.json").read_text(encoding="utf-8"))["ok"] is False


def test_mapping_keeps_decimal_epoch_and_uses_first_frame_not_spawn(tmp_path):
    item = capture(tmp_path)
    item._clock_samples = [clock_sample()]
    item._input_start_ns = video._seconds_ns("1700000000.123456")
    item._first_filter_pts_ns = video._seconds_ns("0.066667")
    mapped = item._mapping({"packets":[{"pts_time":"0.067000"}]})
    assert mapped["valid"] is True
    assert mapped["video_zero_wall_ns"] == 1_700_000_000_190_123_000
    assert mapped["video_zero_perf_ns"] == 2_190_123_000
    assert mapped["filter_packet_delta_ns"] == 333_000
    assert mapped["uncertainty_ms"] >= 1000/15


def test_mapping_refuses_missing_pts_and_clock_discontinuity(tmp_path):
    item = capture(tmp_path)
    item._input_start_ns = 1_700_000_000_000_000_000
    item._first_filter_pts_ns = 0
    item._clock_samples = [clock_sample(), clock_sample(wall=1_700_000_001_100_000_000, perf=3_000_000_000)]
    mapped = item._mapping({})
    assert mapped["valid"] is False
    assert "FIRST_VIDEO_PACKET_PTS_MISSING" in mapped["invalid_reasons"]
    assert "WALL_CLOCK_DISCONTINUITY_OR_DRIFT_OVER_50MS" in mapped["invalid_reasons"]


def test_mapping_rejects_muxer_pts_shift(tmp_path):
    item = capture(tmp_path)
    item._clock_samples = [clock_sample()]
    item._input_start_ns = 1_700_000_000_000_000_000
    item._first_filter_pts_ns = 0
    mapped = item._mapping({"packets":[{"pts_time":"0.2"}]})
    assert mapped["valid"] is False
    assert "FILTER_AND_CONTAINER_FIRST_PTS_DIFFER" in mapped["invalid_reasons"]


def test_graceful_stop_probes_and_remuxes_without_reencoding(tmp_path, monkeypatch, fake_binaries):
    monkeypatch.setattr(video.subprocess, "Popen", FakeProcess)
    item = capture(tmp_path)
    assert item.start()["ok"]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "-c" in command:
            assert command[command.index("-c")+1] == "copy"
            Path(command[-1]).write_bytes(b"fake-mp4")
            return subprocess.CompletedProcess(command, 0)
        metadata = {"packets":[{"pts_time":"0.000000"}], "streams":[{"codec_type":"video"}],
                    "format":{"start_time":"0.000000","duration":"1.000000"}}
        return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")

    monkeypatch.setattr(video.subprocess, "run", run)
    result = item.stop()
    assert result["ok"] is True
    assert result["mapping"]["valid"] is True
    assert result["graceful_stop"] is True
    assert result["remux"]["reencoded"] is False
    assert item.mkv.is_file() and item.mp4.is_file()
    assert (tmp_path / "ffprobe-mkv.json").is_file()
    assert (tmp_path / "ffprobe-mp4.json").is_file()
    assert item.stop() is result
    assert len(calls) == 3


def test_unexpected_process_exit_is_reported(tmp_path, monkeypatch, fake_binaries):
    monkeypatch.setattr(video.subprocess, "Popen", FakeProcess)
    item = capture(tmp_path)
    assert item.start()["ok"]
    item.process.returncode = 9
    assert item.health()["error"] == "FFMPEG_EXITED_UNEXPECTEDLY"
    monkeypatch.setattr(item, "_probe", lambda *args: {"ok":False})
    result = item.stop()
    assert result["ok"] is False
    assert result["graceful_stop"] is False
    assert result["remux"]["skipped"] is True


def test_normal_stop_refuses_invalid_mapping(tmp_path, monkeypatch, fake_binaries):
    monkeypatch.setattr(video.subprocess, "Popen", FakeProcess)
    item = capture(tmp_path)
    assert item.start()["ok"]
    monkeypatch.setattr(item, "_probe", lambda *args: {"ok":True,"metadata":{"packets":[{"pts_time":"0.5"}]}})
    monkeypatch.setattr(item, "_remux", lambda: {"ok":True})
    result = item.stop()
    assert result["ok"] is False
    assert result["mapping"]["valid"] is False


def test_stderr_regex_matches_zero_and_decimal_input_start():
    assert video.START_RE.search("  Duration: N/A, start: 1793160000.123456, bitrate: 3456 kb/s").group(1) == "1793160000.123456"
    assert video.FIRST_FRAME_RE.search("[Parsed_showinfo_0] n:   0 pts:      0 pts_time:0 duration:66667").group(2) == "0"


@pytest.mark.skipif(not video.DEFAULT_FFMPEG.is_file(), reason="bundled FFmpeg is unavailable")
def test_synthetic_timestamp_gap_survives_encoding_and_remux(tmp_path):
    """Real codec check using a generated pattern; never opens gdigrab."""
    item = capture(tmp_path)
    item.ffmpeg = video.DEFAULT_FFMPEG
    item.ffprobe = video.DEFAULT_FFMPEG.with_name("ffprobe.exe")
    command = [str(item.ffmpeg), "-hide_banner", "-n", "-loglevel", "error",
               "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=2",
               "-vf", r"setpts=PTS+gte(N\,10)/TB", "-c:v", "libx264", "-preset", "ultrafast",
               "-tune", "zerolatency", "-bf", "0", "-fps_mode", "passthrough",
               "-enc_time_base:v", "1:1000000", "-avoid_negative_ts", "disabled", str(item.mkv)]
    encoded = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert encoded.returncode == 0, encoded.stderr
    assert item._remux()["ok"]
    for media in (item.mkv, item.mp4):
        probe = subprocess.run([str(item.ffprobe), "-v", "error", "-select_streams", "v:0",
                                "-show_packets", "-show_format", "-of", "json", str(media)],
                               capture_output=True, text=True, timeout=20)
        assert probe.returncode == 0, probe.stderr
        metadata = json.loads(probe.stdout)
        pts = [float(packet["pts_time"]) for packet in metadata["packets"]]
        assert len(pts) == 30  # No CFR frame duplication, frame deletion or accelerated playback.
        assert max(b-a for a,b in zip(pts, pts[1:])) >= 1.05
        assert 2.9 <= float(metadata["format"]["duration"]) <= 3.1
