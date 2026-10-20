"""#77 的两把锁，以及它们的反例。

第一把（写盘锁）在 conftest 里：pytest 会话期间测试进程不许写现网 `data/`。
本文件只验尺子，不碰生产文件：

- 正向：本次会话四条生效路径都在测试临时根内；
- 反向：手造一份"硬编码仓库 data/ 绝对路径"的清单，同一把尺子必须逐条报红；
- 机制：在 tmp 里搭一份"生产布局"（`data/approvals.json`），挂同一个写盘审计
  钩子，真实的追加打开 / `os.replace` / 项目根 preference 写入必须被拦住，且文件
  一字节不变；钩子用完立刻禁用（tmp 目录会话结束会被 pytest 删除，钩子若还开着
  会把清理动作也拦下）。

第二把（静态锁）是本票标题那一句：`backend/tests/test_*.py` 不许出现
`if __name__ == "__main__":` 入口。离开 pytest 就没有 conftest 的四条重定向，
2026-10-17 那次越界就是从这个入口进去的；单跑请用
`python -m pytest backend/tests/test_xxx.py`。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _prod_data_guard as guard  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent

WATCHED_LABELS = {
    "审批票据",
    "意见反馈",
    "反馈原文(feedback_storage)",
    "反馈原文(preference_analyzer)",
    "偏好摘要",
}


def test_this_session_effective_paths_are_inside_the_test_tmp_root():
    """正向：conftest 指走的四条路径，现在真的都在临时根里。"""
    pairs = guard.effective_watched_paths()
    assert {label for label, _ in pairs} == WATCHED_LABELS, (
        f"生效路径的覆盖面变了：{[label for label, _ in pairs]}")
    bad = guard.path_violations(pairs, guard.active().allowed_roots)
    assert not bad, guard.format_violations("运行中的 pytest 会话", bad)


def test_the_ruler_flags_a_hardcoded_production_path():
    """反向控制：硬编码仓库 data/ 的绝对路径必须逐条报红。

    喂的是手工清单，不是真把 env 改成仓库路径：本目录同时是生产机，把真库路径放进
    环境里哪怕一瞬，后面任何一次 flush 都可能照着那一刻的环境写盘。
    """
    repo = guard.repo_root().resolve()
    hostile = [
        ("审批票据", str(repo / "data" / "approvals.json")),
        ("意见反馈", str(repo / "data" / "user_feedback.json")),
        ("反馈原文", str(repo / "data" / "feedback.json")),
        ("偏好摘要", str(repo / "data" / "preference.txt")),
        ("反馈原文(项目根历史位)", str(repo / "feedback.json")),
    ]
    bad = guard.path_violations(hostile, guard.active().allowed_roots)
    assert len(bad) == len(hostile), (
        f"反例没被逐条报出来，上面那条绿不算证据：{bad}")
    assert {label for label, _, _ in bad} == {label for label, _ in hostile}
    for _label, _path, reason in bad:
        assert "仓库目录" in reason, reason


def test_the_ruler_does_not_flag_legitimate_temp_paths(tmp_path):
    """同一条尺子对合法指法必须报空，否则上面"报红"只是因为谁都被报红。"""
    pairs = [("审批票据", str(tmp_path / "approvals.json")),
             ("意见反馈", str(tmp_path / "user_feedback.json"))]
    assert guard.path_violations(pairs, guard.active().allowed_roots) == []


def test_empty_or_unreadable_effective_path_fails_closed():
    """读不到 = 违规：空值、空白、读取失败占位串都不许悄悄放过。"""
    for raw in ("", "   ", "<读取失败 RuntimeError: boom>"):
        bad = guard.path_violations([("审批票据", raw)],
                                    guard.active().allowed_roots)
        assert bad and bad[0][2].startswith("空值或读取失败"), (raw, bad)


def test_relative_path_that_resolves_into_the_repo_is_flagged(monkeypatch):
    """相对路径常量也算：CWD 在仓库根时，`feedback.json` 会落到项目根历史位。"""
    monkeypatch.chdir(guard.repo_root())
    bad = guard.path_violations([("反馈原文", "feedback.json")],
                                guard.active().allowed_roots)
    assert bad and "仓库目录" in bad[0][2], bad


def test_strict_and_timer_tiers_match_the_writer_they_watch():
    """approvals/feedback 走严格对账；preference 只进定时器档。"""
    strict, timer = guard.production_candidates()
    strict_names = {path.name for paths in strict.values() for path in paths}
    timer_names = {path.name for paths in timer.values() for path in paths}
    assert {"approvals.json", "user_feedback.json", "feedback.json"} <= strict_names
    assert "preference.txt" in timer_names
    assert not strict_names & timer_names, (
        f"偏好摘要同时进了两档：{strict_names & timer_names}")


def test_the_live_reader_flags_a_production_value_without_writing(monkeypatch):
    """端到端反例：把 env 与模块常量在**上下文里**指到现网路径，同一读取器必须报红。

    只读不写；`monkeypatch.context()` 退出即还原，所以不会把生产路径带给 autouse
    的收尾检查，也不会碰磁盘。这一条证明 `check_effective_paths()` 读的是生效值，
    不是"env 名字在不在"。
    """
    import app.feedback_storage as fs

    repo = guard.repo_root().resolve()
    prod_approvals = str(repo / "data" / "approvals.json")
    prod_feedback = str(repo / "data" / "feedback.json")
    with monkeypatch.context() as m:
        m.setenv("APPROVALS_DB_PATH", prod_approvals)
        m.setattr(fs, "FEEDBACK_FILE", prod_feedback)
        pairs = guard.effective_watched_paths()
        bad = guard.path_violations(pairs, guard.active().allowed_roots)

    assert dict(pairs)["审批票据"] == prod_approvals
    assert dict(pairs)["反馈原文(feedback_storage)"] == prod_feedback
    labels = {label for label, _, _ in bad}
    assert {"审批票据", "反馈原文(feedback_storage)"} <= labels, bad


def test_stamp_sees_a_same_size_rewrite_through_mtime(tmp_path):
    """只对字节不够：把同一份内容原样写回一遍，字节相同、mtime 变了也算写过。"""
    target = tmp_path / "approvals.json"
    target.write_text("{}", encoding="utf-8")
    before = guard._stamp(target)
    os.utime(target, ns=(before[0] + 1_000_000, before[0] + 1_000_000))
    after = guard._stamp(target)
    assert after[1] == before[1], "这条判据要演的是同字节"
    assert after[0] != before[0], "mtime 变了却没被看见：同字节重写会漏"
    assert guard.snapshot_changes(
        {("审批票据", str(target)): before},
        {("审批票据", str(target)): after}) != []


def test_the_audit_hook_blocks_a_write_into_a_watched_data_dir(tmp_path):
    """机制反例：同一把钩子挂在 tmp 的"生产布局"上，三种写入口全被拦住。

    这里一次都没有碰真 production：假仓库在 tmp_path 下，钩子只认它的 data/ 与
    项目根历史位。钩子装上去摘不掉，用 `disable()` 关掉，否则 pytest 清理 tmp
    目录时的 unlink 也会被当成违规。
    """
    fake = tmp_path / "repo"
    data = fake / "data"
    data.mkdir(parents=True)
    target = data / "approvals.json"
    target.write_text("{}", encoding="utf-8")
    before = target.read_bytes()

    sink = []
    handle = guard.install_write_audit_hook(fake, sink=sink)
    try:
        with pytest.raises(guard.ProductionDataWriteError):
            target.open("a", encoding="utf-8").write("x")
        # rename 的两个端点都算：从 data/ 搬走（src）与搬进 data/（dst）
        with pytest.raises(guard.ProductionDataWriteError):
            os.replace(target, tmp_path / "moved-out.json")
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        with pytest.raises(guard.ProductionDataWriteError):
            os.replace(outside, data / "moved-in.json")
        with pytest.raises(guard.ProductionDataWriteError):
            (fake / "preference-u_probe.txt").write_text("x", encoding="utf-8")
    finally:
        handle.disable()

    assert target.read_bytes() == before, "被拦下的写竟然改了字节"
    assert not (tmp_path / "moved-out.json").exists()
    assert not (data / "moved-in.json").exists()
    assert not (fake / "preference-u_probe.txt").exists()
    assert len(sink) == 4, f"拦截记录应为四条：{sink}"


def _main_guard_lines(source):
    """源码里 `if __name__ == "__main__":` 的行号（两种比较顺序都认）。"""
    tree = ast.parse(source)
    hits = []
    for node in tree.body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if len(test.ops) != 1 or len(test.comparators) != 1:
            continue
        if not isinstance(test.ops[0], ast.Eq):
            continue
        left, right = test.left, test.comparators[0]

        def is_name(n):
            return isinstance(n, ast.Name) and n.id == "__name__"

        def is_main(n):
            return isinstance(n, ast.Constant) and n.value == "__main__"

        if (is_name(left) and is_main(right)) or (is_main(left) and is_name(right)):
            hits.append(node.lineno)
    return hits


def test_the_standalone_entry_scanner_has_teeth():
    """静态锁自己的反例：正反两种比较顺序都要认出来，别的比较不许误报。"""
    assert _main_guard_lines("if __name__ == '__main__':\n    pass\n") == [1]
    assert _main_guard_lines("if '__main__' == __name__:\n    pass\n") == [1]
    assert _main_guard_lines("if __name__ == 'x':\n    pass\n") == []


def test_no_test_file_can_be_exec_directly_outside_pytest():
    """#77：仓内测试文件不许有直接执行入口，离开 pytest 就没有数据隔离。"""
    offenders = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        hits = _main_guard_lines(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "这些测试文件还留着 `if __name__ == \"__main__\"` 直接执行入口："
        f"{offenders}。直接 `python backend/tests/<file>` 不会加载 conftest，"
        "四条数据路径会落回现网 data/（2026-10-17 已实证写进 12 条票据）。"
        "单跑请用 `python -m pytest backend/tests/<file>`。")
