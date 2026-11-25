"""#225：审计自由文本里的绝对路径必须被掩码（2026-11-26 收口）。

为什么钉：审计的 `detail` 是调用方可填的自由文本，一句异常原文就能把本机盘符路径写进
audit.jsonl。出口层（response）早就有 pathmask 这道网，**落盘层没有**；而审计的要求比出口
更高——它长期留存、会被管理台展示、会被导出。

判据分两半：路径必须隐、凭据必须仍然隐（补路径不能把凭据那条网挤掉），且普通句子
一个字都不许动（掩码不能变成"什么都删"）。
"""
import pytest

from app.core.audit import _scrub_text


def test_windows_and_posix_paths_are_masked():
    # 用通用形状而不是真机路径：仓库里不许出现「某台机器才认得」的绝对路径
    # （test_v0391_thinking_mapping.py::test_no_test_source_carries_a_machine_absolute_path
    # 会当场判红）。要验的是「盘符路径被掩」，账号名是哪一串无关。
    win = r"写入失败: C:\\Users\\someone\\proj\\data\\x.json"
    out = _scrub_text(win)
    assert "someone" not in out and "proj" not in out, f"本机路径原样落盘：{out}"
    assert "写入失败" in out, "句子其余部分要照读——掩码不是删句"

    posix = "/home/runner/work/fenver/data/y.json 不可读"
    out = _scrub_text(posix)
    assert "fenver" not in out, f"POSIX 路径原样落盘：{out}"
    assert "不可读" in out


def test_credentials_are_still_hidden():
    """补路径掩码不许把凭据那条网挤掉。"""
    assert "abcdefghijklmnop" not in _scrub_text("上游 401：Bearer abcdefghijklmnop 已过期")
    assert "abcdef123456" not in _scrub_text("调用失败：sk-live-abcdef123456")


def test_a_plain_sentence_passes_through_untouched():
    for plain in ("普通句子没有路径", "auth.login.failed", "配额已用完，请稍后再试"):
        assert _scrub_text(plain) == plain, f"掩码误伤了普通文本：{plain!r}"


def test_empty_input_stays_empty():
    assert _scrub_text("") == ""