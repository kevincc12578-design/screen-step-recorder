"""Selection behavior only: no windows, hooks or recording are started."""

import pytest

from af_recorder.gui import choose_target, fresh_target, is_autoform_window, ordered_windows, window_label


def window(hwnd, exe, title="", pid=None):
    return {"hwnd": hwnd, "pid": pid or hwnd + 100, "exe": exe, "title": title,
            "monitor_rect": [0, 0, 1920, 1080], "dpi": 96}


def test_titles_cannot_identify_autoform():
    explorer = window(1, r"C:\Windows\explorer.exe", "autoform自动化")
    recorder = window(2, r"D:\Python314\pythonw.exe", "AutoForm 视频与步骤录制器")
    deepseek = window(3, r"C:\DeepSeek.exe", "AutoForm R13 / AFFormingUI.exe")
    assert not any(is_autoform_window(item) for item in [explorer, recorder, deepseek])
    assert choose_target([explorer, recorder, deepseek]) is None


def test_exact_executable_r13_prioritized_over_title_and_other_release():
    explorer = window(1, r"C:\Windows\explorer.exe", "AAA autoform自动化")
    older = window(2, r"D:\AutoForm\R10F\bin\AFFormingUI.exe", "AAA")
    r13 = window(3, r"D:\Program Files\AutoForm\AFplus\R13F\bin\AFFormingUI.exe", "ZZZ")
    similar = window(4, r"D:\AFFormingUI.exe.bak", "AutoForm R13")
    assert [item["hwnd"] for item in ordered_windows([explorer, older, r13, similar])][:2] == [3, 2]
    assert choose_target([explorer, older, r13, similar])["hwnd"] == 3
    assert "AFFormingUI.exe" in window_label(r13)
    assert "PID 103" in window_label(r13)


def test_wrong_automatic_explorer_choice_is_not_preserved():
    explorer = window(1, r"C:\Windows\explorer.exe", "autoform自动化")
    r13 = window(3, r"D:\R13F\AFFormingUI.exe", "R13")
    assert choose_target([explorer, r13], explorer)["hwnd"] == 3
    assert choose_target([explorer], explorer) is None


def test_explicit_other_application_choice_is_preserved():
    explorer = window(1, r"C:\Windows\explorer.exe", "autoform自动化")
    r13 = window(3, r"D:\R13F\AFFormingUI.exe", "R13")
    assert choose_target([explorer, r13], explorer, preserve_manual=True)["hwnd"] == 1


def test_hwnd_pid_reuse_is_rejected_and_current_monitor_is_returned():
    old = window(3, r"D:\R13F\AFFormingUI.exe", "R13", pid=20)
    moved = {**old, "monitor_rect": [-1920, 0, 0, 1080], "dpi": 120}
    assert fresh_target(old, [moved])["monitor_rect"] == [-1920, 0, 0, 1080]
    with pytest.raises(RuntimeError, match="目标窗口"):
        fresh_target(old, [{**old, "pid": 21}])
    with pytest.raises(RuntimeError, match="目标窗口"):
        fresh_target(old, [])


def test_executable_matching_is_case_insensitive():
    assert is_autoform_window(window(3, r"d:\r13f\bin\aFfOrMiNgUi.ExE"))
    assert not is_autoform_window(window(4, None, "AutoForm R13"))
