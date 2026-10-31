"""Build an evidence timeline without inferring AutoForm process stages."""
from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def video_time(perf_ns: int, mapping: dict) -> float | None:
    if not mapping.get("valid") or mapping.get("video_zero_perf_ns") is None:
        return None
    return round((perf_ns - int(mapping["video_zero_perf_ns"])) / 1_000_000_000, 6)


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "待校准"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{sign}{int(hours):02}:{int(minutes):02}:{seconds:06.3f}"


def build_steps(events: list[dict], mapping: dict) -> list[dict]:
    """Retain down/up/drags/keys verbatim; never invent named operations."""
    kinds = {"mouse_down", "mouse_up", "wheel", "key", "mark", "focus", "error"}
    return [
        {**event, "step": index + 1, "video_seconds": video_time(event["perf_ns"], mapping)}
        for index, event in enumerate(e for e in events if e.get("kind") in kinds)
    ]


def write_timeline(out: Path, mapping: dict, summary: dict, events: list[dict] | None = None) -> dict:
    events = read_jsonl(out / "events.jsonl") if events is None else events
    steps = build_steps(events, mapping)
    (out / "steps.json").write_text(json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf-8")
    title = "# 新录制操作时间线\n\n"
    text = [title, "这是原始事件与观察证据索引，未推断工艺阶段或操作是否成功。\n\n",
            f"视频时间映射有效：`{bool(mapping.get('valid'))}`；不确定度：`{mapping.get('uncertainty_ms', '未知')}` ms。\n\n",
            "鼠标按下前的控件来自定时观察缓存；缺失、过期或指针距离过远时不应当作点击目标。\n\n",
            "| 序号 | 视频位置 | 原始动作 | 控件观察（非动作结论） | 截图 |\n",
            "| --- | --- | --- | --- | --- |\n"]
    for step in steps:
        before = step.get("target_before") or {}
        control = before.get("control") or {}
        label = control.get("name") or control.get("class_name") or "未读取"
        if before.get("usable") is False:
            label += "（缓存不可靠）"
        action = step.get("kind", "")
        if step.get("button"):
            action += ":" + str(step["button"])
        if step.get("key"):
            action += ":" + "+".join([*(step.get("modifiers") or []), str(step["key"])])
        if step.get("note"):
            action += ":" + step["note"]
        shots = step.get("screenshots") or {}
        links = " ".join(f"[{name}]({entry['path']})" for name, entry in shots.items() if entry.get("path"))
        safe = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
        text.append(f"| {step['step']} | {format_time(step['video_seconds'])} | {safe(action)} | {safe(label)} | {links} |\n")
    text.extend(["\n完整数据：`events.jsonl`、`observations.jsonl`、`video.json`、`session.json`。\n",
                 "\n视频覆盖目标所在显示器；普通键入字符未写入键盘事件。画面中可见文字仍会出现在视频和截图。\n"])
    (out / "步骤.md").write_text("".join(text), encoding="utf-8")
    return {"steps": len(steps), "events": len(events)}
