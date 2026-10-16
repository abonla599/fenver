"""工单 #33：一条 `failed` 却还带着可信工具断点的任务，钱只许付一次。

缺陷形状（审查员复现出来的那一格）：编排器**先认领**（`_claim_execution`）再跑工具，
工具可能挂起（→ `waiting_user` + `kind=="tool_suspension"` 的游标），这一趟也可能抛。
`_record_failure` 写 `TaskStatus.FAILED`，而 `set_status` **刻意留着**那份游标（它是
"上次停在哪儿"的证据）。留下来的结果就是一个"代码嘴上说不可续、身体却从第 1 步重跑"
的状态：FAILED 分支（`orchestrator.py:164-165`）**一眼都不看游标**，直接掉进通用续跑，
于是已经付过钱的第 1~3 步（查店、加购、发验证码）再烧一遍，验证码再发一遍。

本文件先原样把它跑红（`test_a_failed_task_with_a_trusted_breakpoint_does_not_re_pay_...`），
再钉住选定的语义：

1. `failed` + **可信同形**的 `tool_suspension` 游标 + `current_subtask` 未动 → 从断点接，
   并且走与 `waiting_user` 续跑**同一套**判据（声明过 suspends、断点带得过重启、答案非空、
   归属闸、认领机制）；不重新规划、不重跑更早的子任务、不第二次付费。
2. 游标不可信（种类不对 / 子任务下标飘了 / 断点读不懂 / 工具不再声明可挂起 / 失败发生在
   任何断点存在之前）→ **拒**，给一句机器可读的理由，要求一次刻意的重新开始。拒是对的，
   重复扣钱不是。
3. 已经不是一次都别的路径：断点**接回去过**（工具真的被递了答案）之后才坏的，不许把同一
   句答案再递一次给同一个工具——那是把已经落地的外部副作用再落一遍。
4. A4 的卡片要的判别器：`end_reason` ∈ {failed, cancelled_by_timeout, cancelled_by_user}，
   以前这件事只有 `error` 里一句中文能说。

一条贯穿全文件的纪律：**判据不许由被测对象自己申报**。所有"花了几次钱"的证据都记在夹具
闭包那几本假账上（`spent` / `resumed` / `llm`），外加 `app.core.usage` 那本真账；
没有一条断言读 `task` 对象的字段来证明"没重跑"。

⚠️ 本机作业纪律：这个仓库目录**同时是生产机**。本文件每条用例的任务表都在
`tmp_path` 里（`isolated_tasks`），退出时清空的是那一份临时文件；最后一条用例把
`resolve_all_data_paths()` 逐条量在临时根内，并直接对仓库真 `data/tasks.json` 的
字节 + mtime 前后对一遍，反例喂的是**手工造的**清单，绝不把仓库路径放进环境。
"""
import asyncio
import copy
import json
import os
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

import app.agents.orchestrator as orch_mod
import app.tools.executor as ex
from app.agents import task_store as ts
from app.agents.orchestrator import TOOL_SUSPENSION_KIND, Orchestrator
from app.core import usage
from app.tools.registry import tools_registry
from app.tools.response import ToolSuspended

_TAG = "tool_call"
_GOAL = "点一杯咖啡"
_UID = "u-33"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"


def _call(name: str, arguments: dict) -> str:
    """模型在 TaskAgent 那条降级路上"要调工具"时的写法（与 task_agent 的判据同形）。"""
    return f"<{_TAG}>" + json.dumps({"name": name, "arguments": arguments},
                                    ensure_ascii=False) + f"</{_TAG}>"


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件；进出清空进程级存储（抄 A3/A5 那份夹具的理由）。

    环境变量只指向 `tmp_path`，**绝不**指向仓库里那份真 `data/tasks.json`。
    """
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture
def t33_tools():
    """道具箱：一条真会挂起的工具 + 一条**没**声明可挂起的同名形状工具。

    三本账全在闭包里（判据问的是"这个世界里第 1~3 步被付了几次"）：
    - `spent`  每一笔 = 一次真跑了第 1~3 步（查店 / 加购 / 发验证码）；
    - `resumed` 每一次"从断点接上"时递进工具的整包；
    - `calls`  每一次进过工具函数体的调用（含被挂起的那一次）。
    """
    spent: list = []
    resumed: list = []
    calls: list = []

    def t33_gate(item: str = "拿铁", resume: dict = None):
        calls.append(("t33_gate", item, resume))
        if resume is None:
            spent.append("step1-3")        # 第 1~3 步：每一步都真花钱
            raise ToolSuspended("请输入短信验证码",
                                {"step": 3, "item": item, "collected": {"shop": "甲"}})
        resumed.append(copy.deepcopy(resume))
        assert isinstance(resume.get("state"), dict), "续跑没带断点"
        return f"已提交订单（{resume['state']['item']}），答案={resume['answer']}"

    def t33_unsure(item: str = "拿铁", resume: dict = None):
        """没声明 suspends 的工具：递断点进去会被执行器拒掉（这条判据也要有牙）。"""
        calls.append(("t33_unsure", item, resume))
        spent.append("step1-3-undeclared")
        raise ToolSuspended("要继续吗", {"step": 1})

    entries = {
        "t33_gate": {"function": t33_gate, "description": "下一杯咖啡（要验证码）",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        "t33_unsure": {"function": t33_unsure, "description": "没声明可挂起",
                       "parameters": {"type": "object",
                                      "properties": {"item": {"type": "string"}}},
                       "available": None, "needs_user": False,
                       "requires_approval": False, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    ledger_saved = dict(ex._FREQ_LEDGER)
    try:
        yield {"spent": spent, "resumed": resumed, "calls": calls}
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous
        ex._FREQ_LEDGER.clear()
        ex._FREQ_LEDGER.update(ledger_saved)


@pytest.fixture
def stub(monkeypatch):
    """三条模型路 + **注入故障的那一刀**，全记在闭包账上。

    `boom["on"]` 让**下一回**"把断点递回工具"那一句在返回前抛出（= 认领已经成功、
    断点一次都没递回去）。这正是工单里写的配方："raise between claim and tool
    completion"。它是单发的，且 `fired` 计数供用例自证前提真的成立（防空测）。
    `task_boom["on"]` 让下一回子任务模型调用抛出（= 断点**已经**递回去了才坏）。
    """
    ledger = {"llm": [], "boom": {"on": False, "fired": 0},
              "task_boom": {"on": False, "fired": 0}, "usage": []}

    real_execute = orch_mod.execute_tool_detailed

    def guarded_execute(tool_name, arguments, user_id=None, granted_ticket=None,
                        resume=None):
        if ledger["boom"]["on"]:
            ledger["boom"]["on"] = False
            ledger["boom"]["fired"] += 1
            raise RuntimeError("注入：认领之后、断点接回去之前炸了")
        return real_execute(tool_name, arguments, user_id=user_id,
                            granted_ticket=granted_ticket, resume=resume)

    monkeypatch.setattr(orch_mod, "execute_tool_detailed", guarded_execute)

    def plan_llm(model, messages, **kw):
        ledger["llm"].append("plan")
        return json.dumps(["选店并下单"], ensure_ascii=False)

    def task_llm(model, messages, **kw):
        ledger["llm"].append("task")
        # 与 llm_client 同一个口径：调用当场记一次账（真账本，不是假账）
        usage.record_call(user_id=_UID, provider_id="p", paid_by="user",
                          prompt_tokens=10, completion_tokens=5, total_tokens=15)
        if ledger["task_boom"]["on"]:
            ledger["task_boom"]["on"] = False
            ledger["task_boom"]["fired"] += 1
            raise RuntimeError("注入：断点已经接回去了，子任务这一步才炸")
        already = any(str(m.get("content", "")).startswith("工具执行结果")
                      for m in messages)
        if already:
            return "拿到了，继续"
        return _call("t33_gate", {"item": "拿铁"})

    def summary_llm(model, messages, **kw):
        ledger["llm"].append("summary")
        usage.record_call(user_id=_UID, provider_id="p", paid_by="user",
                          prompt_tokens=20, completion_tokens=8, total_tokens=28)
        return "最终汇总"

    monkeypatch.setattr("app.agents.planner.get_llm_response", plan_llm)
    monkeypatch.setattr("app.agents.task_agent.get_llm_response", task_llm)
    monkeypatch.setattr("app.agents.orchestrator.get_llm_response", summary_llm)
    return ledger


def _orchestrator() -> Orchestrator:
    return Orchestrator(model="m")


def _park_through_the_breakpoint(orch, uid=_UID, subtasks=("选店并下单",)):
    """真链路跑到"停在工具断点上等人"：第 1~3 步已付费，游标由编排器自己写下。"""
    task = ts.Task(goal=_GOAL, subtasks=list(subtasks), user_id=uid)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=uid)
    assert res["status"] == "waiting_user", res
    return task, res


def _break_after_the_claim(orch, stub, task, answer="8888", uid=_UID):
    """工单 #33 的配方：带答案进来 → 认领成功 → **在断点接回去之前**抛。

    回来的那一趟把状态写成 failed，而游标按 A1 的纪律留着。这里断言的是"注入真的
    发生了"（`boom.fired == 1`），否则下面所有判据都只是在测一条没走过的路。
    """
    stub["boom"]["on"] = True
    before_fired = stub["boom"]["fired"]
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=uid, user_answer=answer)
    assert stub["boom"]["fired"] == before_fired + 1, "前提没成立：那一刀没落下去"
    assert res["status"] == "failed", res
    return res


def _assert_33_shape_present(task):
    """#33 的形状确实在盘上/内存里：failed + 可信游标 + 子任务下标没动。

    注意这条**修复前后都必须为真**：游标是证据，工单明确不许用"失败时清空游标"
    来修（那只是把账据撕了，重复付费还在）。
    """
    assert task.status is ts.TaskStatus.FAILED, task.status
    assert task.resume_cursor["kind"] == TOOL_SUSPENSION_KIND, task.resume_cursor
    assert task.can_resume is False, "can_resume 由不变式跟着状态走，这一格没被改坏"
    assert task.current_subtask == task.resume_cursor["subtask_index"]


def test_a_failed_task_with_a_trusted_breakpoint_does_not_re_pay_steps_1_to_3(
        t33_tools, stub):
    """⭐ 复现 + 判据：坏掉的那一趟之后，再来一次**不带答案**的请求不许把前三步重烧。

    改动前红在哪一句：FAILED 分支不看游标、直接进通用续跑，于是 `t33_gate` 又被从零
    调了一次 → `spent` 从 1 笔变 2 笔（就是审查员贴的那两行输出）。
    改动后：带答案才接（`answer_required`），没答案就一句实话，什么都不跑。
    """
    orch = _orchestrator()
    task, parked = _park_through_the_breakpoint(orch)
    assert t33_tools["spent"] == ["step1-3"], t33_tools["spent"]

    _break_after_the_claim(orch, stub, task)
    _assert_33_shape_present(task)

    spent_before = list(t33_tools["spent"])
    calls_before = list(t33_tools["calls"])
    resumed_before = list(t33_tools["resumed"])
    llm_before = list(stub["llm"])
    usage_before = copy.deepcopy(usage.snapshot())

    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID)

    assert t33_tools["spent"] == spent_before, (
        f"SPENT before={len(spent_before)} after={len(t33_tools['spent'])} "
        f"→ 已经付过钱的第 1~3 步又被烧了一遍：{t33_tools['spent']}")
    assert t33_tools["calls"] == calls_before, f"工具又被跑了一次：{t33_tools['calls']}"
    assert t33_tools["resumed"] == resumed_before
    assert stub["llm"] == llm_before, f"这一趟还叫了模型：{stub['llm'][len(llm_before):]}"
    assert usage.snapshot() == usage_before, "账本动了：这一趟根本没资格花钱"
    # 而且要说得出话：这是一次有理由的"没动"，不是静默失败
    assert res.get("accepted") is False, res
    assert res.get("reason") == "answer_required", res
    # 给 A4 的那三格必须互相自洽：能接（带答案）≠ 此刻在等人（can_resume 由 A1 不变式钉着）
    assert res.get("fresh_start_required") is False, res
    assert res.get("can_resume") is False, "failed 记录不许自称 waiting_user"
    assert res.get("end_reason") == "failed", res
    assert "question" in res, "接得回去的断点必须把那句问题带回去，不然用户答什么"
    assert res["status"] == "failed", res
    assert task.status is ts.TaskStatus.FAILED, "拒的那一趟不许把状态改掉"


def test_a_failed_task_with_a_trusted_breakpoint_resumes_from_the_breakpoint(
        t33_tools, stub):
    """⭐ 语义 1：failed + 可信游标 + **带着答案** → 从断点接上，第 1~3 步一次都不许多。

    改动前红在哪一句：FAILED 分支不看游标 → 通用续跑把整条子任务从第 1 步重跑 →
    `spent` 变 2 笔，而且 `resumed` 是空的（断点压根没被接，等于替用户重发验证码）。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    _break_after_the_claim(orch, stub, task, answer="8888")
    _assert_33_shape_present(task)
    assert stub["boom"]["fired"] == 1, "前提没成立：注入的那一刀没落下去"
    resumed_before = list(t33_tools["resumed"])
    assert t33_tools["spent"] == ["step1-3"]

    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert t33_tools["spent"] == ["step1-3"], (
        f"从断点续跑却重烧了前三步：{t33_tools['spent']}")
    assert len(t33_tools["resumed"]) == len(resumed_before) + 1, (
        f"断点没被接回去（或接了不止一次）：{t33_tools['resumed']}")
    packet = t33_tools["resumed"][-1]
    assert packet["answer"] == "8888"
    assert packet["state"]["step"] == 3, packet       # 接的就是挂起时那份断点
    assert res["status"] == "completed", res
    assert task.status is ts.TaskStatus.COMPLETED
    # 不重新规划：这条任务是手搓出来的（`_park_through_the_breakpoint`），规划那一条模型路
    # 全程**一次都不该被叫**；叫了就是重新规划了一遍=整趟重烧。
    assert stub["llm"].count("plan") == 0, f"又规划了一遍：{stub['llm']}"


def test_a_failed_breakpoint_still_needs_a_non_empty_answer(t33_tools, stub):
    """⭐ 语义 1 的那套判据之一：**答案非空**才接。空答案 = 什么都不跑。

    与 `waiting_user` 那一路同一句话（编排器里既有那格 fail-closed），不许"先跑了再说"，
    因为一次没有答案的续跑等于替用户把验证码编了，而且那一次是真花钱的。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    _break_after_the_claim(orch, stub, task)
    spent_before = list(t33_tools["spent"])
    calls_before = list(t33_tools["calls"])
    snapshot_before = copy.deepcopy(task.to_dict())

    for answer in ("", "   ", None):
        res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID,
                       user_answer=answer)
        assert res.get("reason") == "answer_required", (answer, res)
        assert res.get("accepted") is False, (answer, res)

    assert t33_tools["spent"] == spent_before, f"空答案还是把钱花了：{t33_tools['spent']}"
    assert t33_tools["calls"] == calls_before, "空答案却把工具又跑了一次"
    assert task.to_dict() == snapshot_before, "空答案的那一趟改动了任务"


@pytest.mark.parametrize("mutate,expect_reason,expect_code", [
    pytest.param(lambda c: c.pop("kind"), "breakpoint_untrusted", "wrong_kind",
                 id="wrong-kind"),
    pytest.param(lambda c: c.__setitem__("kind", "asked_a_question"),
                 "breakpoint_untrusted", "wrong_kind", id="other-kind"),
    pytest.param(lambda c: c.__setitem__("subtask_index", 7),
                 "breakpoint_untrusted", "stale_subtask_index", id="stale-index"),
    pytest.param(lambda c: c.__setitem__("resume_state", "不是 dict"),
                 "breakpoint_untrusted", "unreadable_state", id="unreadable-state"),
    pytest.param(lambda c: c.__setitem__("resume_state", {"step": 3, "t": (1, 2)}),
                 "breakpoint_untrusted", "unreadable_state", id="state-not-carriable"),
    pytest.param(lambda c: c.__setitem__("tool_name", "t33_unsure"),
                 "breakpoint_untrusted", "tool_not_suspendable", id="tool-undeclared"),
    pytest.param(lambda c: c.__setitem__("tool_name", ""),
                 "breakpoint_untrusted", "unreadable_state", id="no-tool-name"),
])
def test_an_untrusted_failed_breakpoint_is_refused_rather_than_rerun(
        t33_tools, stub, mutate, expect_reason, expect_code):
    """⭐ 语义 2：游标不可信就**拒**，绝不悄悄重跑已经付过钱的步骤。

    每一种不可信都单独造（种类不对 / 下标飘了 / 断点读不懂 / 带不过重启 / 工具不再声明
    可挂起 / 连工具名都没有）。改动前红在哪一句：FAILED 分支一律不看游标，
    这些记录全部掉进通用续跑 → `spent` 多出几笔。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    _break_after_the_claim(orch, stub, task)
    cursor = copy.deepcopy(task.resume_cursor)
    mutate(cursor)
    task.resume_cursor = cursor
    ts.save(task)

    spent_before = list(t33_tools["spent"])
    calls_before = list(t33_tools["calls"])
    snapshot_before = copy.deepcopy(task.to_dict())
    usage_before = copy.deepcopy(usage.snapshot())

    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert res.get("accepted") is False, res
    assert res.get("reason") == expect_reason, res
    assert res.get("refuse_code") == expect_code, res
    assert res.get("fresh_start_required") is True, res
    assert t33_tools["spent"] == spent_before, (
        f"不可信的游标（{expect_code}）却把步骤重跑了：{t33_tools['spent']}")
    assert t33_tools["calls"] == calls_before, "不可信的游标却把工具又跑了一次"
    assert usage.snapshot() == usage_before, "拒的那一趟动了账本"
    assert task.to_dict() == snapshot_before, "拒的那一趟改动了任务（连证据都不许动）"


def test_a_state_that_cannot_be_written_to_disk_at_all_is_not_trusted(t33_tools, stub):
    """`_state_carries` 的另一半：断点里揣着一个**连写都写不出去**的值。

    上面参数化那条用的是"能写、但 JSON 往返回来的不是同一份"（元组 → 数组）；这一条用的是
    "连 `json.dumps` 都过不了"（一个 object 实例），它只能活在内存里——而那恰恰是最贵的
    一格：进程还在、游标还在、形状看着对，答案递进去什么都接不上。判据必须同样回
    `unreadable_state`，并且一把都不许多花。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    _break_after_the_claim(orch, stub, task)
    task.resume_cursor = {**task.resume_cursor, "resume_state": {"obj": object()}}
    spent_before = list(t33_tools["spent"])
    calls_before = list(t33_tools["calls"])

    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert res.get("reason") == "breakpoint_untrusted", res
    assert res.get("refuse_code") == "unreadable_state", res
    assert t33_tools["spent"] == spent_before, f"接不上的断点却重跑了步骤：{t33_tools['spent']}"
    assert t33_tools["calls"] == calls_before, "接不上的断点却把工具又跑了一次"


def test_a_failure_before_any_breakpoint_existed_is_not_rerun_silently(
        t33_tools, stub):
    """⭐ 语义 2 的最后一种：失败发生在**任何断点存在之前** → 同样不许悄悄重跑。

    这条钉的是老语义"failed → running 断点重跑"里最贵的那一半：没有断点就等于
    "从第 1 步再来一遍"，而那一步可能已经付过钱。改动前红在哪一句：它真的又从第 1 步
    跑了一遍（`spent` 多一笔、还重新叫了模型）。
    """
    orch = _orchestrator()
    task = ts.Task(goal=_GOAL, subtasks=["选店并下单"], user_id=_UID)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    stub["task_boom"]["on"] = True
    first = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID)
    assert first["status"] == "failed", first
    assert task.resume_cursor is None, "这条记录压根没有断点（#33 的另一半形状）"

    spent_before = list(t33_tools["spent"])
    llm_before = list(stub["llm"])
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert res.get("reason") == "breakpoint_untrusted", res
    assert res.get("refuse_code") == "no_breakpoint", res
    assert t33_tools["spent"] == spent_before, f"没有断点却重跑了工具：{t33_tools['spent']}"
    assert stub["llm"] == llm_before, f"没有断点却又叫了模型：{stub['llm'][len(llm_before):]}"


def test_a_consumed_breakpoint_is_never_handed_to_the_tool_twice(t33_tools, stub):
    """⭐ 语义 3：断点**已经接回去过**（工具真的收到了答案）之后才坏的，不许再递一次。

    再递一次 = 把同一个外部副作用再落一遍（订单再提交一次 / 验证码再发一次）。
    形状是：带答案续跑 → 工具真的收到 resume 那一包 → 子任务那一步才炸 → failed。
    改动前红在哪一句：FAILED 分支不看游标，于是那一趟既重烧了第 1~3 步，也**没有**
    任何一句"这个断点已经用过了"的话——重复副作用完全不可见。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    assert t33_tools["spent"] == ["step1-3"]
    assert t33_tools["resumed"] == []

    stub["task_boom"]["on"] = True
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")
    assert res["status"] == "failed", res
    assert len(t33_tools["resumed"]) == 1, (
        "前提没成立：断点没被真的递进工具，这条用例就是空测")
    assert t33_tools["spent"] == ["step1-3"]
    assert task.status is ts.TaskStatus.FAILED
    assert task.resume_cursor["kind"] == TOOL_SUSPENSION_KIND

    res2 = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="9999")

    assert t33_tools["resumed"] == t33_tools["resumed"][:1], (
        f"同一个断点被递了两次（副作用落了两次）：{t33_tools['resumed']}")
    assert t33_tools["spent"] == ["step1-3"], (
        f"已消费的断点还把前三步重烧了一遍：{t33_tools['spent']}")
    assert res2.get("accepted") is False, res2
    assert res2.get("reason") == "breakpoint_untrusted", res2
    assert res2.get("refuse_code") == "consumed_breakpoint", res2
    assert task.status is ts.TaskStatus.FAILED, "拒的那一趟不许改动状态"


def test_a_request_that_read_waiting_user_cannot_claim_a_failed_record(
        t33_tools, stub, monkeypatch):
    """⭐ #33 自己开出来的那道窄门：在 waiting_user 上读到的答案，不许拿去接一条 failed。

    会合是确定的、不靠 sleep：X 那趟在 `run` 的第 1.9 步（认领之前、状态**已经**读完之后）
    会调一次惰性清扫，这里把那一跳换成"另一条请求 Y 完整地跑一遍"。Y 把断点接走并消费、
    然后在子任务那一步炸掉 → 任务变成 `failed` + `consumed` 的游标。这一刻 X 手里那份
    `pending_resume` 是**旧读**：按 #33 之前一版的写法（"failed 也算断点态"就一律放行）
    X 会接着把同一个答案再递一次给同一个工具 = 同一个外部副作用落两遍。
    判据：X 必须被 `breakpoint_taken` 挡在门外，`resumed` 只有一条、`spent` 只有一笔。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    assert t33_tools["spent"] == ["step1-3"] and t33_tools["resumed"] == []

    real_sweep = Orchestrator._sweep_timed_out_parks
    hook = {"fired": 0}

    def hooked(self, current_task_id=None):
        if hook["fired"] == 0:
            hook["fired"] = 1
            stub["task_boom"]["on"] = True      # Y 接上断点之后，子任务那一步炸
            other = Orchestrator(model="m")
            got = other.run(goal=_GOAL, task_id=task.task_id, user_id=_UID,
                            user_answer="8888")
            assert got["status"] == "failed", got      # 前提：Y 真的把它跑成了 failed
        return real_sweep(self, current_task_id)

    monkeypatch.setattr(Orchestrator, "_sweep_timed_out_parks", hooked)

    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="1234")

    assert hook["fired"] == 1, "前提没成立：那条并发请求压根没插进来，这条用例是空测"
    assert res.get("accepted") is False, res
    assert res.get("reason") == "breakpoint_taken", res
    assert len(t33_tools["resumed"]) == 1, (
        f"同一个断点被递了两次（副作用落了两次）：{t33_tools['resumed']}")
    assert t33_tools["spent"] == ["step1-3"], (
        f"旧读的那一趟把前三步重烧了：{t33_tools['spent']}")
    assert task.status is ts.TaskStatus.FAILED, task.status


def test_the_failed_resume_still_refuses_a_stranger_through_the_ownership_gate(
        t33_tools, stub):
    """语义 1 里那句"归属闸"：别人的 failed 断点，猜着答案也接不动。

    非属主走的是既有那条"当作不存在 → 新建一条自己的任务"。这条判据要抓的是另一种
    偷懒实现：为了"能续 failed"而在归属之外另开一条口子（那等于把任务表变成 uuid
    探测器 + 让别人替 A 把验证码花了）。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch, uid="u-33-owner")
    _break_after_the_claim(orch, stub, task, uid="u-33-owner")
    _assert_33_shape_present(task)
    before = copy.deepcopy(task.to_dict())
    spent_before = list(t33_tools["spent"])

    res = orch.run(goal="乙自己的目的", task_id=task.task_id, user_id="u-33-stranger",
                   user_answer="8888")

    assert res["task_id"] != task.task_id, "陌生人的请求竟以甲的任务 id 往下跑了"
    assert task.to_dict() == before, "陌生人的请求改动了甲的任务"
    # 陌生人那一趟建的是一条**新**任务（既有语义），甲的那一步一次都没被跑：
    # spent 只能多出陌生人自己那一条新任务的那一笔，且 resumed 必须还是空的。
    assert t33_tools["resumed"] == [], "陌生人的答案接了甲的断点"
    assert len(t33_tools["spent"]) == len(spent_before) + 1, (
        f"陌生人那一趟该只花自己新任务的一笔：{t33_tools['spent']}")


def test_end_reason_discriminates_why_a_task_ended(t33_tools, stub):
    """⭐ 语义 4：A4 卡片要的机器可读判别器（以前只有 `error` 里一句中文）。

    三条都是走**真的**跳转写出来的：failed（注入那一刀）、cancelled-by-timeout
    （A5 的作废那一路）、cancelled-by-user（循环里的取消检查 / mark_cancelled）。
    每条都同时判三处：内存字段、盘上那一份、以及**出口响应体里那一格**（审查员量到的
    正是 `{"status":"cancelled","reason":null,"accepted":null}`——那句话没有键可读）。
    """
    orch = _orchestrator()

    # ① failed
    task_failed, _ = _park_through_the_breakpoint(orch)
    failed_body = _break_after_the_claim(orch, stub, task_failed)
    assert task_failed.end_reason == "failed", task_failed.end_reason
    assert failed_body["end_reason"] == "failed", failed_body

    # ② cancelled-by-timeout（A5 那一路：挂太久 → cancelled + 游标清空）
    timeout_window = 3600.0
    parked = ts.Task(goal=_GOAL, subtasks=["选店并下单"], user_id=_UID)
    ts.task_store[parked.task_id] = parked
    parked.set_status(ts.TaskStatus.RUNNING)
    parked.wait_for_user(question="请输入短信验证码", cursor={
        "kind": TOOL_SUSPENSION_KIND, "tool_name": "t33_gate",
        "arguments": {"item": "拿铁"}, "resume_state": {"step": 3},
        "subtask_index": 0})
    parked.waiting_since = (ts._now() - ts.timedelta(seconds=timeout_window + 1)
                            ).isoformat()
    ts.save(parked)
    assert ts.expire_parked_task(parked, timeout_seconds=timeout_window) is True, \
        "前提没成立：这条停点没被判成超时"
    assert parked.end_reason == "cancelled_by_timeout", parked.end_reason
    assert parked.resume_cursor is None
    body = orch.run(goal=_GOAL, task_id=parked.task_id, user_id=_UID,
                    user_answer="8888")
    assert body["status"] == "cancelled", body
    assert body["end_reason"] == "cancelled_by_timeout", body

    # ③ cancelled-by-user（终态之前的正常取消）
    by_user = ts.Task(goal=_GOAL, subtasks=["选店并下单"], user_id=_UID)
    ts.task_store[by_user.task_id] = by_user
    by_user.set_status(ts.TaskStatus.RUNNING)
    by_user.mark_cancelled()
    assert by_user.end_reason == "cancelled_by_user", by_user.end_reason
    body3 = orch.run(goal=_GOAL, task_id=by_user.task_id, user_id=_UID)
    assert body3["status"] == "cancelled", body3
    assert body3["end_reason"] == "cancelled_by_user", body3

    # 盘上那一份与内存同一个形状（判的是落盘，不是内存）
    on_disk = json.loads(Path(os.environ["TASKS_DB_PATH"]).read_text(
        encoding="utf-8"))["tasks"]
    assert on_disk[parked.task_id]["end_reason"] == "cancelled_by_timeout", on_disk
    assert on_disk[by_user.task_id]["end_reason"] == "cancelled_by_user"
    assert on_disk[task_failed.task_id]["end_reason"] == "failed"

    # 未定论的记录不许带着它：一个键都不许多（additive-compat 的那一半）
    fresh = ts.Task(goal=_GOAL, subtasks=["a"], user_id=_UID)
    assert fresh.end_reason is None
    assert "end_reason" not in fresh.to_dict(), "没定论的记录多出一格 = 键集漂移"
    assert ts.Task.from_dict(fresh.to_dict()).to_dict() == fresh.to_dict()


def test_the_end_reason_round_trips_and_rejects_a_bogus_value(isolated_tasks):
    """判别器要过一次重启还在；盘上那句读不懂的一律不认（不猜）。"""
    task = ts.Task(goal=_GOAL, subtasks=["a"], user_id=_UID)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.mark_cancelled()
    assert task.end_reason == "cancelled_by_user"
    ts.restore(path=str(isolated_tasks))
    assert ts.get_task(task.task_id).end_reason == "cancelled_by_user", "过不了重启"

    # 手改成认不出的值：读回 None，而不是"猜一个"，也不许带走同一条记录里其余的好键
    isolated_tasks.write_text(json.dumps({"tasks": {
        "t_bad": {"task_id": "t_bad", "user_id": "u-1", "goal": "g", "subtasks": [],
                  "status": "cancelled", "cancelled": True, "end_reason": {"不": "对"},
                  "created_at": "2026-10-01T09:00:00"},
    }}), encoding="utf-8")
    assert ts.restore(path=str(isolated_tasks)) == 1, "坏的一格不许把整条记录带走"
    bad = ts.get_task("t_bad")
    assert bad.end_reason is None, bad.end_reason
    assert bad.status is ts.TaskStatus.CANCELLED

    # 一个已知值要读得回来
    isolated_tasks.write_text(json.dumps({"tasks": {
        "t_ok": {"task_id": "t_ok", "user_id": "u-1", "goal": "g", "subtasks": [],
                 "status": "cancelled", "cancelled": True,
                 "end_reason": "cancelled_by_timeout",
                 "created_at": "2026-10-01T09:00:00"},
    }}), encoding="utf-8")
    ts.restore(path=str(isolated_tasks))
    assert ts.get_task("t_ok").end_reason == "cancelled_by_timeout"


def test_leaving_a_terminal_state_clears_the_stale_end_reason():
    """failed → running（合法的断点续跑）之后不许还挂着上一轮的 end_reason。

    判别器说的是"**为什么**这条任务 ended"，一个正在跑的任务带着 failed = 下一个读它的人
    （A4 的卡片）会把"跑起来了"念成"又失败了"。
    """
    task = ts.Task(goal=_GOAL, subtasks=["a"], user_id=_UID)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.set_status(ts.TaskStatus.FAILED)
    assert task.end_reason == "failed"
    task.set_status(ts.TaskStatus.RUNNING)          # 跳转表里那条既有的合法边
    assert task.end_reason is None, "续跑之后还在报上一轮的失败原因"
    task.mark_cancelled()
    assert task.end_reason == "cancelled_by_user"


def test_the_transition_table_gained_no_edge_and_cancelled_still_has_none():
    """⭐ 语义 5 的表：这一票一条边都没加（`failed → running` 本来就在）。

    `cancelled` 出边必须还是空集——A5 的"超时作废"整座地基压在"cancelled 谁也再跑不动"
    上；给它加任何出边就是把 #33 那扇门从另一侧重新打开。
    """
    for target in ts.TaskStatus:
        assert ts.can_transition(ts.TaskStatus.CANCELLED, target) is False, (
            f"cancelled 多了一条出边 → {target.value}")
    assert ts.ALLOWED_TRANSITIONS[ts.TaskStatus.FAILED] == frozenset(
        {ts.TaskStatus.RUNNING}), ts.ALLOWED_TRANSITIONS[ts.TaskStatus.FAILED]
    assert ts.can_transition(ts.TaskStatus.FAILED, ts.TaskStatus.RUNNING) is True, (
        "failed → running 被删了：可信断点的续跑无路可走")
    assert ts.can_transition(ts.TaskStatus.WAITING_USER, ts.TaskStatus.RUNNING) is True


def _async_client():
    """ASGITransport + `raise_app_exceptions=False`（本仓跨请求证据的既有写法，不用 TestClient）。"""
    from app.main import app
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver", timeout=120.0)


def test_the_endpoint_path_resumes_a_failed_breakpoint_without_re_paying(
        t33_tools, stub, monkeypatch, tmp_path):
    """⭐ 跨请求证据（走 /v1/agent/orchestrate 本体，不是直接调方法）。

    三趟真请求：① 建任务并停在断点 → ② 带答案进来、在断点接回去之前炸（#33 形状）→
    ③ 再带答案进来。判据：第 1~3 步只烧一次、断点只被接一次、③  completed，
    且 GET /v1/tasks/{id} 那一格 end_reason 读得出来。
    """
    from app.core.auth import AuthStore
    import app.core.authz as authz

    store = AuthStore(path=str(tmp_path / "users.json"))
    monkeypatch.setattr(authz, "auth_store", store)
    monkeypatch.setenv("AUTH_MODE", "enforced")
    monkeypatch.setenv("ACCESS_TOKEN", "boot-token")
    _, token = store.register(username="工单三十三", password="correct-horse-battery")
    headers = {"Authorization": "Bearer " + token}

    async def scenario():
        async with _async_client() as c:
            me = await c.get("/v1/auth/me", headers=headers)
            assert me.status_code == 200, me.text
            uid = me.json()["user_id"]
            r1 = await c.post("/v1/agent/orchestrate", json={"goal": _GOAL},
                              headers=headers)
            tid = r1.json()["task_id"]
            stub["boom"]["on"] = True          # ② 认领之后、断点接回去之前炸
            r2 = await c.post("/v1/agent/orchestrate",
                              json={"goal": _GOAL, "task_id": tid,
                                    "user_answer": "8888"}, headers=headers)
            r3 = await c.post("/v1/agent/orchestrate",
                              json={"goal": _GOAL, "task_id": tid,
                                    "user_answer": "8888"}, headers=headers)
            detail = await c.get(f"/v1/tasks/{tid}", headers=headers)
            return uid, r1.json(), r2.json(), r3.json(), detail.json()

    uid, b1, b2, b3, detail = asyncio.run(scenario())
    assert b1["status"] == "waiting_user", b1
    assert b2["status"] == "failed", b2
    assert b2["end_reason"] == "failed", b2
    assert t33_tools["spent"] == ["step1-3"], (
        f"端点那一路把前三步重烧了：{t33_tools['spent']}")
    assert len(t33_tools["resumed"]) == 1, t33_tools["resumed"]
    assert b3["status"] == "completed", b3
    assert detail["end_reason"] is None, detail      # completed：这一格还没"定论原因"
    assert ts.get_task(b1["task_id"]).user_id == uid


def _paths_outside(pairs, allowed_roots):
    """这些实际落点里有几条不该要它：不在允许的临时根内，或干脆落在仓库里。"""
    repo = os.path.realpath(str(REPO_ROOT)) + os.sep
    offenders = []
    for label, raw in pairs:
        resolved = os.path.realpath(str(raw))
        inside = any(os.path.realpath(r) == resolved
                     or resolved.startswith(os.path.realpath(r) + os.sep)
                     for r in allowed_roots)
        if not inside or resolved.startswith(repo):
            offenders.append((label, resolved))
    return offenders


def test_this_file_writes_only_inside_its_own_temp_root(t33_tools, stub, tmp_path):
    """⚠️ 本机作业纪律：跑完整条 #33 链路之后，每一个可变数据落点仍在临时根内。

    正例：`resolve_all_data_paths()`（`app/core/paths.py:119`）逐条量。
    反例：同一把尺子量一份**手工造的**"指回仓库真库"的清单必须报红——否则上面那句
    绿是没牙的。反例绝不真的把 `TASKS_DB_PATH` 改成仓库路径（本目录同时是生产机，
    那一刻之后任何一次 flush 都会写进真 `data/tasks.json`）。
    最后一句直接对仓库那份文件比字节 + mtime：路径判据说"不在这里"，这句证明"这里
    没被写过"。
    """
    orch = _orchestrator()
    before_bytes = REPO_DATA_TASKS.read_bytes()
    before_mtime = REPO_DATA_TASKS.stat().st_mtime_ns

    task, _ = _park_through_the_breakpoint(orch, uid=_UID)
    _break_after_the_claim(orch, stub, task)
    orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")
    for tid in list(ts.task_store):        # 走一次真删除，逼出 teardown 里那类 flush
        ts.delete_task(tid)

    allowed = [str(tmp_path), tempfile.gettempdir()]
    offenders = _paths_outside(resolve_pairs(), allowed)
    assert not offenders, f"可变数据落到了临时根之外：{offenders}"

    hostile = ("任务清单", os.path.realpath(str(REPO_DATA_TASKS)))
    assert hostile in _paths_outside([hostile], allowed), "尺子没牙：反例都没报出来"
    assert _paths_outside([("任务清单", os.environ["TASKS_DB_PATH"])], allowed) == [], \
        "合法那条也被报了，上面那句红就没有区分度"

    assert REPO_DATA_TASKS.read_bytes() == before_bytes, "真库的字节被改过"
    assert REPO_DATA_TASKS.stat().st_mtime_ns == before_mtime, "真库被原样重写过一次"


def resolve_pairs():
    """把权威清单取回来（单独一个函数，好让上面那条读起来只有一件事）。"""
    from app.core.paths import resolve_all_data_paths
    return [(label, path) for label, path in resolve_all_data_paths()]
