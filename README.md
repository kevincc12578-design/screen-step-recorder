# 视频与步骤录制器

从 `D:\ai库\autoform自动化` 提取的独立录制器，复制日期：2026-10-31。

GitHub 仓库：[screen-step-recorder](https://github.com/kevincc12578-design/screen-step-recorder)。运行环境为 Windows；仓库包含源码、启动入口、说明及测试，录制输出不进入版本控制。

双击 [启动录制器.cmd](启动录制器.cmd) 打开中文界面。详细操作见 [录制器使用说明.md](录制器使用说明.md)。

- 同步保存屏幕视频、鼠标与快捷键事件、步骤时间线、控件观察和截图。
- 自动优先识别 AutoForm R13，也保留原版的手动选择窗口功能。
- F9 标记步骤，F10 停止保存；默认不录音频。
- 新录像保存到当前项目的 `录制结果/session-.../`。

## 文件与依赖

`af_recorder/` 为程序源码；`tests/` 为原有测试；`tools/recorder_smoke.py` 为可手动运行的录制自检。启动器使用本机 `D:\Python314\pythonw.exe`；Python 依赖见 `requirements-recorder.txt`。FFmpeg/FFprobe 使用本机已有的 `C:\Users\Administrator\.codex\tools\video-analyzer-runtime\ffmpeg\ffmpeg-9.0.2-essentials_build\bin`，也支持从 PATH 查找。本目录未打包 Python 或 FFmpeg 运行环境。

## 迁入验证

14 个来源文件复制后均完成 SHA-256 比对，记录见 `来源文件校验.json`（记录复制时的哈希）。程序代码和启动入口保持原版，使用说明随后更新了项目路径并移除了旧 AutoForm 演示任务的专用提示。原项目文件保留原位；历史录像和 AutoForm 自动执行代码未迁入。

在本目录完成依赖检查 `python -m af_recorder --doctor`，结果 `ok: true`；原有 42 项测试全部通过。本次未开始实际录屏。
