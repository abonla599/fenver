"""v0.35.0 工单 #153：`plan_add` 在信息不足时真的挂起。

这几格是"判据 1 能实跑"的前提：全仓此前没有任何工具会 `raise ToolSuspended`，于是
编排器 `can_resume ⟺ waiting_user` 那一格永远造不出来。本文件只钉 `plan_add` 的
**问与不问**；执行器的第三态协议归 `test_v0350_a3_*`，超时窗口归 A5，两处都不在这里重测。

问的边界（与 `builtin_tools.plan_add` 的注释同一份口径）：
- 问：`at`、`day` 都为空，且 `text` 里没有任何可识别的时间词。
- 不问：`at` 或 `day` 有一个；或 `text` 里能认出时间词（认得出具体日子/时刻就采用）。
- 续跑：只认 state 里的原文与已定部分 + answer；answer 认不出就落今天并留 note，
  绝不再问第二次。
"""
from collections import defaultdict
from datetime import date, timedelta

import pytest

import app.tools.builtin_tools  # noqa: F401  导入期注册：不导入就没有真工具可派发
from app.core import schedule
from app.tools import executor as ex
from app.tools.builtin_tools import plan_add
from app.tools.response import ToolSuspended


@pytest.fixture(autouse=True)
def _fresh_freq_ledger(monkeypatch):
    """频控账本是进程级全局：每条用例一份，否则前一条的计数会带进后一条。"""
    monkeypatch.setattr(ex, "_FREQ_LEDGER", defaultdict(list))


def _tomorrow() -> str:
    return (date.fromisoformat(schedule.today()) + timedelta(days=1)).isoformat()


# ---------- ① 缺时间信息真的挂起 ----------

def test_a_bare_reminder_raises_a_suspension_with_the_raw_text(isolated_schedule):
    """「记得提醒我交周报」没有任何时间锚点：必须停在断点问一句，而不是替用户编今天。"""
    with pytest.raises(ToolSuspended) as caught:
        plan_add("u-1", "记得提醒我交周报")
    suspended = caught.value
    assert suspended.prompt.strip(), "挂起却没给出要问用户的话"
    assert suspended.state == {"text": "记得提醒我交周报", "at": "", "day": ""}
    assert schedule.plan("u-1") == [], "挂起时就已经写进日程 = 问之前先把事办了"


def test_the_suspension_survives_the_executor_as_a_resumable_breakpoint(isolated_schedule):
    out = ex.execute_tool_detailed("plan_add", {"text": "交周报"}, user_id="u-1")
    assert out.suspended is True and out.ok is False, out.text
    assert out.tool_ran is True, "异常从函数体里出来，答案确实递进去过"
    assert out.text.startswith("⏸"), out.text
    assert out.suspension["can_resume"] is True, out.suspension
    assert out.suspension["resume_state"]["text"] == "交周报"
    assert schedule.plan("u-1") == [], "挂起时不该已经落一条"


# ---------- ② 带 resume 的续跑能写完 ----------

def test_a_resume_completes_the_item_and_reads_the_answer(isolated_schedule):
    first = ex.execute_tool_detailed("plan_add", {"text": "交周报"}, user_id="u-1")
    resumed = ex.execute_tool_detailed(
        "plan_add", {"text": "交周报"}, user_id="u-1",
        resume={"state": first.suspension["resume_state"], "answer": "明天九点"})

    assert resumed.ok is True and resumed.suspended is False, resumed.text
    rows = schedule.plan("u-1", _tomorrow())
    assert [r["text"] for r in rows] == ["交周报"], rows
    assert rows[0]["at"] == "09:00", rows


def test_an_unusable_resume_answer_lands_today_once_and_says_so(isolated_schedule):
    """有界失败：answer 仍认不出就按今天写完并留 note，绝不问第二次。"""
    first = ex.execute_tool_detailed("plan_add", {"text": "交周报"}, user_id="u-1")
    resumed = ex.execute_tool_detailed(
        "plan_add", {"text": "交周报"}, user_id="u-1",
        resume={"state": first.suspension["resume_state"], "answer": "随便"})

    assert resumed.ok is True and resumed.suspended is False, resumed.text
    assert "note" in resumed.text and schedule.today() in resumed.text, resumed.text
    assert [r["text"] for r in schedule.plan("u-1", schedule.today())] == ["交周报"]


# ---------- ③ 信息充足时不挂起 ----------

@pytest.mark.parametrize("arguments", [
    {"text": "开会", "at": "15:00"},
    {"text": "开会", "day": "@TOMORROW"},
    {"text": "明天九点开会"},
    {"text": "明天下午三点开会"},
    {"text": "下周三交材料"},
])
def test_enough_information_never_suspends(isolated_schedule, arguments):
    args = dict(arguments)
    if args.get("day") == "@TOMORROW":
        args["day"] = _tomorrow()
    out = ex.execute_tool_detailed("plan_add", args, user_id="u-1")
    assert out.suspended is False, out.text
    assert out.ok is True, out.text


def test_a_relative_day_argument_lands_instead_of_suspending(isolated_schedule):
    """模型把「明天」直接塞进 day 是常态：认出它就换算，不因此打断用户。"""
    out = ex.execute_tool_detailed("plan_add", {"text": "开会", "day": "明天"}, user_id="u-1")
    assert out.ok is True and out.suspended is False, out.text
    assert [r["text"] for r in schedule.plan("u-1", _tomorrow())] == ["开会"]


# ---------- ④ 续跑计次：用执行器现有那一道闸 ----------

def test_resume_and_first_run_consume_the_same_frequency_budget(isolated_schedule):
    """首跑与续跑共用 `executor._FREQ_LIMITS` 那一档。

    第 1 次挂起，随后 `cap - 1` 次续跑各计一次，第 `cap + 1` 次必须被现有闸门挡在
    函数体之外。cap 从表里现读，不在测试里抄第二份。
    """
    _, cap = ex._FREQ_LIMITS["plan_add"]
    assert cap >= 2, "少于两次的档连首跑加一次续跑都装不下"
    first = ex.execute_tool_detailed("plan_add", {"text": "第 1 条"}, user_id="u-freq")
    assert first.suspended is True, first.text
    packet = {"state": first.suspension["resume_state"], "answer": "9点"}

    for attempt in range(2, cap + 1):
        allowed = ex.execute_tool_detailed(
            "plan_add", {"text": "第 1 条"}, user_id="u-freq", resume=packet)
        assert allowed.ok is True, f"第 {attempt} 次被挡：{allowed.text}"

    blocked = ex.execute_tool_detailed(
        "plan_add", {"text": "第 1 条"}, user_id="u-freq", resume=packet)
    assert blocked.ok is False and blocked.tool_ran is False, blocked.text
    assert "过于频繁" in blocked.text, blocked.text


# ---------- ⑤ 重启后断点仍在：走 task_store 的 restore ----------

def test_the_breakpoint_survives_task_store_restore(isolated_schedule, tmp_path, monkeypatch):
    """断点随任务落盘，重启后读回来的同一份 state 仍能把日程续完。"""
    from app.agents import task_store as ts

    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    try:
        first = ex.execute_tool_detailed("plan_add", {"text": "交周报"}, user_id="u-1")
        state = first.suspension["resume_state"]
        task = ts.Task(goal="提醒交周报", subtasks=["记一条日程"], user_id="u-1")
        ts.task_store[task.task_id] = task
        task.set_status(ts.TaskStatus.RUNNING)
        task.wait_for_user(first.suspension["prompt"], cursor={
            "kind": "tool_suspension", "tool_name": "plan_add",
            "arguments": {"text": "交周报"}, "resume_state": state,
            "subtask_index": 0,
        })

        ts.restore(path=str(path))                 # 等价于重启进程
        back = ts.get_task(task.task_id)
        assert back is not None and back.status is ts.TaskStatus.WAITING_USER
        cursor = back.resume_cursor
        assert cursor["resume_state"] == state, "断点没带过重启"
        assert ex._can_resume("plan_add", cursor["resume_state"]) is True

        resumed = ex.execute_tool_detailed(
            "plan_add", cursor["arguments"], user_id="u-1",
            resume={"state": cursor["resume_state"], "answer": "明天九点"})
        assert resumed.ok is True, resumed.text
        assert [r["text"] for r in schedule.plan("u-1", _tomorrow())] == ["交周报"]
    finally:
        ts.task_store.clear()
