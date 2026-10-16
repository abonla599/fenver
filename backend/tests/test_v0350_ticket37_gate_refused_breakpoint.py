"""工单 #37：一次**被闸门挡下**的续跑（工具压根没收到答案）不许把断点判成"已消费"。

工单 #33（上一条提交）为了"同一个答案绝不被递两遍"，在把断点交回工具之后就往游标里
写了一笔 `consumed`。判据用的是"这一句返回了"而不是"这一句真的跑进了工具"——于是
审查员量到的形状是：**被频控拒掉的续跑**（工具一次都没被执行）也被标成 consumed，
用户答了、闸门说不、断点却当场烧了，接下来只能重新开始。

```
case5 (frequency cap = 1): tool_received_answer = 0   consumed = true
retry → accepted:false / refuse_code=consumed_breakpoint / fresh_start_required:true
```

更糟的是 `tools/executor.py:326-329`：审批票据接不过一次挂起，所以一条同时声明
`suspends:True` 与 `requires_approval:True` 的工具**必然**是"被拒 + 被烧"——那正是
A4 要交付的第一个真工具，必须在 A4 之前关掉。

本文件的不变式（判据都朝这一句）：**工具没收到答案，答案就必须还能再递进去。**
"被闸门拒了、游标仍然可信"**绝不**等于"可以重付第 1~3 步"，也**绝不**重放一个已经落地
的外部副作用——#33 那道门不许因此重新打开（六条不信任代码 + 锁内信任复核照旧有效）。

一条贯穿全文件的纪律：**判据不许由被测对象自己申报。**"答案到底递没递进工具"这件事
记在夹具闭包的三本假账上（`received`=工具收到答案、`spent`=真跑了第 1~3 步、
`calls`=进过函数体），外加执行器新吐出的 `tool_ran`（来自 `_dispatch` 里"func 到底被
调用没有"这一处事实，不是来自 ok/success）。

⚠️ 本机作业纪律：这个仓库目录**同时是生产机**。每条用例的任务表都在 `tmp_path` 里
（`isolated_tasks`），最后一条把 `resolve_all_data_paths()` 逐条量在临时根内，并直接对
仓库真 `data/tasks.json` 的字节 + mtime 前后对一遍，反例喂的是**手工造的**清单。
"""
import copy
import json
import os
import tempfile
from pathlib import Path

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
_UID = "u-37"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"


def _call(name: str, arguments: dict) -> str:
    return f"<{_TAG}>" + json.dumps({"name": name, "arguments": arguments},
                                    ensure_ascii=False) + f"</{_TAG}>"


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件；进出清空进程级存储（抄 A3/#33 那份夹具的理由）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture
def t37_tools():
    """道具箱：一条真会挂起的工具 + 几条专门用来演各道闸门的工具。

    三本假账全在闭包里（判据问的是"这个世界里答案被递进工具几次"）：
    - `received` 每一次"从断点接上"时**真的进到了函数体**的那一包（= tool_received_answer）；
    - `spent`   每一笔 = 一次真跑了第 1~3 步（查店 / 加购 / 发验证码）；
    - `calls`   每一次进过工具函数体的调用（含被挂起、含在体内抛异常的那一次）。
    """
    received: list = []
    spent: list = []
    calls: list = []

    def t37_gate(item: str = "拿铁", resume: dict = None):
        calls.append(("t37_gate", item, resume))
        if resume is None:
            spent.append("step1-3")            # 第 1~3 步：每一步都真花钱
            raise ToolSuspended("请输入短信验证码",
                                {"step": 3, "item": item, "collected": {"shop": "甲"}})
        received.append(copy.deepcopy(resume))  # ⭐ 答案真的进到了工具里
        return f"已提交订单（{resume['state']['item']}），答案={resume['answer']}"

    def t37_boom(item: str = "拿铁", resume: dict = None):
        """续跑时函数体**被调用了**才抛——这是"跑了一半才坏"，副作用可能已落地。"""
        calls.append(("t37_boom", item, resume))
        if resume is None:
            spent.append("step1-3")
            raise ToolSuspended("请输入短信验证码", {"step": 3, "item": item})
        raise RuntimeError("注入：答案已递进工具，函数跑到一半才炸")

    def t37_needs(item: str = "拿铁", resume: dict = None, user_id: str = None):
        """needs_user：没身份就在**函数体之前**被拒（答案递不进去）。"""
        calls.append(("t37_needs", item, resume))
        if resume is None:
            spent.append("step1-3")
            raise ToolSuspended("请输入短信验证码", {"step": 3, "item": item})
        received.append(copy.deepcopy(resume))
        return f"已按 {user_id} 提交订单，答案={resume['answer']}"

    def t37_both(note: str = "x", resume: dict = None):
        """既挂起又需审批（A4 第一个真工具的形状）：本票选项 b 要在**派发处**响亮拒绝。"""
        calls.append(("t37_both", note, resume))
        if resume is None:
            spent.append("step1-3")
            raise ToolSuspended("请输入验证码", {"step": 2})
        received.append(copy.deepcopy(resume))
        return f"审批+挂起本不该走到这里：{resume['answer']}"

    def t37_calc(expression: str = "1+1"):
        """需审批但**不挂起**（演 execute_code 那一族）：批准后照旧该真执行。"""
        calls.append(("t37_calc", expression, None))
        return {"1+1": 2, "2*3": 6}.get(expression, 0)

    entries = {
        "t37_gate": {"function": t37_gate, "description": "下单（要验证码）",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        "t37_boom": {"function": t37_boom, "description": "体内抛错",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        "t37_needs": {"function": t37_needs, "description": "要身份的挂起工具",
                      "parameters": {"type": "object",
                                     "properties": {"item": {"type": "string"}}},
                      "available": None, "needs_user": True,
                      "requires_approval": False, "suspends": True},
        "t37_both": {"function": t37_both, "description": "又挂起又需审批",
                     "parameters": {"type": "object",
                                    "properties": {"note": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": True, "suspends": True},
        "t37_calc": {"function": t37_calc, "description": "算一下（可需审批，不挂起）",
                     "parameters": {"type": "object",
                                    "properties": {"expression": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    ledger_saved = dict(ex._FREQ_LEDGER)
    limits_saved = dict(ex._FREQ_LIMITS)
    try:
        yield {"received": received, "spent": spent, "calls": calls}
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous
        ex._FREQ_LEDGER.clear()
        ex._FREQ_LEDGER.update(ledger_saved)
        ex._FREQ_LIMITS.clear()
        ex._FREQ_LIMITS.update(limits_saved)


@pytest.fixture
def stub(monkeypatch):
    """三条模型路 + **注入故障的那两刀**（抄 #33 那份 stub 的记法）。

    `boom`：下一回"把断点递回工具"那一句在进执行器之前抛出（认领成功、断点一次没递回去）。
    `task_boom`：下一回子任务模型调用抛出（= 续跑**已经返回**、第 3 步的模型才炸）。
    """
    ledger = {"llm": [], "boom": {"on": False, "fired": 0},
              "task_boom": {"on": False, "fired": 0}}

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
        return _call("t37_gate", {"item": "拿铁"})

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


def _fill_freq_gate(tool_name: str, user_id: str, cap: int = 1):
    """把某人的频控账灌到上限，使**下一次**续跑在函数体之前被闸门挡下（= 审查员的 case5）。"""
    import time
    ex._FREQ_LIMITS[tool_name] = (600.0, cap)
    with ex._FREQ_LOCK:
        ex._FREQ_LEDGER[(tool_name, user_id)] = [time.monotonic()] * cap


# ---------- ⭐ 主判据：被闸门拒了的续跑不许烧断点（case5 的原样复现） ----------

def test_a_gate_refused_resume_leaves_the_breakpoint_re_deliverable(t37_tools, stub):
    """工单 #37 本体：频控挡下一次续跑 → 答案从没进工具 → 断点必须还能接。

    改动前红在哪一句：续跑返回 ok=False（闸门拒的）后，`orchestrator.py` 无条件的
    `_consume_breakpoint` 照样落笔 → `consumed=True` → 下一次带答案进来被认成
    "已经接回去过一次"（consumed_breakpoint），要求重新开始。审查员贴的就是这两行。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    assert t37_tools["spent"] == ["step1-3"], t37_tools["spent"]
    assert t37_tools["received"] == []

    # 让续跑在**函数体之前**被频控挡下（case5：cap=1，账已灌满）
    _fill_freq_gate("t37_gate", _UID)
    # 被拒的续跑仍会往下走第 3 步——这一刀让它落成 failed（带着那枚没被消费的断点）
    stub["task_boom"]["on"] = True

    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    # 世界那本账：工具从没收到答案（闸门在函数体之前就 return 了）
    assert t37_tools["received"] == [], (
        f"被频控挡下的续跑却把答案递进了工具：{t37_tools['received']}")
    # 不变式：工具没收到答案 → 答案必须还能再递 → 断点绝不能被标成已消费
    assert not task.resume_cursor.get("consumed"), (
        "工单 #37：被闸门拒下、工具压根没收答案的续跑，却把断点判成已消费——"
        "用户答了、闸门说不、断点当场烧了，接下来只能重新开始")

    # 闸门放行（窗口过去）之后，同一个答案必须还能接回去
    with ex._FREQ_LOCK:
        ex._FREQ_LEDGER.clear()
    stub["task_boom"]["on"] = False
    res2 = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert len(t37_tools["received"]) == 1, (
        f"闸门放行后同一个答案仍接不回去（断点被误烧）：{t37_tools['received']}")
    assert res2["status"] == "completed", res2
    # 不重付第 1~3 步（续跑走的是 resume 那一支，函数体不再 spent）
    assert t37_tools["spent"] == ["step1-3"], (
        f"被拒的续跑重烧了前三步：{t37_tools['spent']}")


# ---------- 执行器层：`tool_ran` 必须来自"func 到底被调用没有"这个世界事实 ----------

def test_tool_ran_is_false_for_every_gate_that_precedes_the_tool_body(t37_tools):
    """把"#37 关心的六种下场"里"没进函数体"的那几道逐一判：`tool_ran` 必须是 False。

    频控、缺身份、需审批+可挂起（选项 b）、断点形状不对——都在 `func(**kwargs)` 之前
    return，答案根本没递进工具（`received` 恒空）。负向控制：若 `_dispatch` 谎报
    tool_ran=True，这几条的第一句立刻红。
    """
    state = {"step": 3, "item": "拿铁", "collected": {"shop": "甲"}}

    # ① 频控挡下
    _fill_freq_gate("t37_gate", "u_freq")
    o = ex.execute_tool_detailed("t37_gate", {"item": "拿铁"}, user_id="u_freq",
                                 resume={"state": state, "answer": "8888"})
    assert o.tool_ran is False, "被频控挡下的续跑不该报告工具已执行"
    assert t37_tools["received"] == []
    with ex._FREQ_LOCK:
        ex._FREQ_LEDGER.clear()

    # ② 缺身份（needs_user）
    o = ex.execute_tool_detailed("t37_needs", {"item": "拿铁"}, user_id="",
                                 resume={"state": state, "answer": "8888"})
    assert o.tool_ran is False, "缺身份被挡下的续跑不该报告工具已执行"
    assert t37_tools["received"] == []

    # ③ 需审批 + 可挂起（选项 b：派发处响亮拒绝）
    o = ex.execute_tool_detailed("t37_both", {"note": "n"}, user_id="u-1",
                                 resume={"state": {"step": 2}, "answer": "8888"})
    assert o.tool_ran is False, "既挂起又需审批的组合绝不该被执行"
    assert t37_tools["received"] == []

    # ④ 断点形状不对（state 不是 dict）
    o = ex.execute_tool_detailed("t37_gate", {"item": "拿铁"}, user_id="u-1",
                                 resume={"state": None, "answer": "8888"})
    assert o.tool_ran is False, "断点形状不对的续跑不该报告工具已执行"
    assert t37_tools["received"] == []


def test_tool_ran_is_true_when_the_tool_body_actually_ran(t37_tools):
    """进了函数体的两种下场（正常返回 / 体内抛异常）：`tool_ran` 必须 True。

    这正是工单 #33 那道门要守住的：答案递进了工具、副作用可能已经落地——这种情况**必须**
    被认成"已消费"。频控那本账在这里是干净的（t37_gate 不挂限流，除非上面那条）。
    """
    state = {"step": 3, "item": "拿铁", "collected": {"shop": "甲"}}

    # ⑤ 正常返回（答案真的递进去了）
    o = ex.execute_tool_detailed("t37_gate", {"item": "拿铁"}, user_id="u_ok",
                                 resume={"state": state, "answer": "8888"})
    assert o.tool_ran is True
    assert len(t37_tools["received"]) == 1, "正常续跑却没把答案记进'收到'那本账"

    # ⑥ 函数体被调用之后才抛（"跑了一半才坏"）——tool_ran 仍 True（保守侧，守 #33）
    before = len(t37_tools["calls"])
    o = ex.execute_tool_detailed("t37_boom", {"item": "拿铁"}, user_id="u_boom",
                                 resume={"state": state, "answer": "8888"})
    assert o.tool_ran is True, "函数体都进了还报'没执行'，等于把跑了一半的副作用当没发生"
    assert len(t37_tools["calls"]) == before + 1


def test_tool_ran_is_true_when_the_tool_suspends_again(t37_tools):
    """续跑之后又差一个答案（再次 raise ToolSuspended）：工具确实收到过答案 → tool_ran True。

    编排器对这一支走的是 `_park_suspension`（写一份**新**游标，旧的 consumed 与否都无所谓），
    所以这条只钉执行器信号本身。
    """
    # t37_gate 第二次挂起用的道具：a3_twice 那种两跳在本文件里用 resume 里带的答案区分。
    o = ex.execute_tool_detailed("t37_gate", {"item": "拿铁"}, user_id="u_again",
                                 resume={"state": {"step": 3, "item": "拿铁"},
                                         "answer": "8888"})
    assert o.tool_ran is True
    assert o.suspended is False        # t37_gate 这一跳是终点
    assert len(t37_tools["received"]) == 1


# ---------- ⭐ 工单 #33 那道门不许重新打开：真的收到答案 → 必须消费 ----------

def test_a_resume_that_actually_delivered_the_answer_is_still_consumed(t37_tools, stub):
    """反向半边：续跑**真的**把答案递进了工具、随后才坏 → 断点必须标成已消费。

    如果为了修 #37 把 `_consume_breakpoint` 整个删掉 / 只在 ok 时消费，这一条红——
    它就是"别让 #33 的门从另一侧重新打开"的守卫：同一个答案第二次进来必须被
    `consumed_breakpoint` 挡在门外，不能把订单再提交一次。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    assert t37_tools["received"] == []

    stub["task_boom"]["on"] = True       # 断点递进工具之后，第 3 步模型才炸 → failed
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")
    assert res["status"] == "failed", res
    assert len(t37_tools["received"]) == 1, "前提没成立：答案没真的递进工具"
    assert task.resume_cursor.get("consumed") is True, \
        "答案已经递进工具（副作用已落地）却不标 consumed——#33 的门被重新打开了"

    res2 = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="9999")
    assert res2.get("accepted") is False, res2
    assert res2.get("refuse_code") == "consumed_breakpoint", res2
    assert len(t37_tools["received"]) == 1, (
        f"同一个断点被递了两次（副作用落了两次）：{t37_tools['received']}")


def test_the_trusted_failed_breakpoint_still_refuses_a_consumed_one(t37_tools, stub):
    """锁内信任复核（`orchestrator.py:503,542-551`）那一族的直接判据：consumed 一律不接。

    手工把一枚可信游标标成 consumed 后，failed 分支必须回 `consumed_breakpoint`，且
    `spent`/`received` 一本都不许多——"被闸门拒了、游标仍可信"绝不允许顺带把第 1~3 步
    或已落地的副作用重放一遍。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    _break_then_fail(orch, stub, task)    # 制造一条 failed + 未消费的可信游标
    # 现在模拟"这一枚真的被接回去过"：打上 consumed
    task.resume_cursor = {**task.resume_cursor, "consumed": True}
    ts.save(task)

    spent_before = list(t37_tools["spent"])
    received_before = list(t37_tools["received"])
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert res.get("refuse_code") == "consumed_breakpoint", res
    assert res.get("fresh_start_required") is True, res
    assert t37_tools["spent"] == spent_before, "已消费的断点却重跑了第 1~3 步"
    assert t37_tools["received"] == received_before, "已消费的断点却把答案又递进工具"


def _break_then_fail(orch, stub, task, answer="8888"):
    """带答案进来 → 在**进执行器之前**抛（#33 的 boom）→ 落成 failed、游标未消费。"""
    stub["boom"]["on"] = True
    res = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer=answer)
    assert res["status"] == "failed", res
    return res


# ---------- 审批那一半（选项 b）：既挂起又需审批的组合，派发处响亮拒绝 ----------

def test_a_suspends_plus_approval_tool_is_refused_loudly_even_with_a_ticket(t37_tools):
    """选项 b：`suspends`+`requires_approval` 这一组合现在不被支持，无论有没有票据。

    这就是"关在派发处"的那一半：A4 若真要把一张票据带过挂起，得先让 approval 票据能跨
    请求跟着断点走（动 approval.py 与游标形状），而不是让这条组合悄悄上线成一个必然被
    拒又被烧的死局。改动前红在哪一句：带着已消费的 allow 票据，这一支能过审批闸门并
    **真的挂起**（suspended=True / func 被调用），于是"批准的是 A、执行的是 B"那条纪律
    接不上、断点也接不回去。
    """
    from app.core import approval
    req = approval.create_approval_request("t37_both", {"note": "n"}, user_id="u-1")
    ticket = approval.consume_ticket(req["ticket_id"], "allow", "u-1")
    assert ticket is not None

    o = ex.execute_tool_detailed("t37_both", {"note": "n"}, user_id="u-1",
                                 granted_ticket=ticket)
    assert o.tool_ran is False, "既挂起又需审批的工具被真执行了（选项 b 没生效）"
    assert o.suspended is False, "这一组合不该交出挂起（它压根接不回去）"
    assert o.ok is False
    # 响亮的话：说清是"两者同时声明"这一组合不支持，而不是含糊的缺审批
    assert "suspends" in o.text and "requires_approval" in o.text, o.text
    assert "工单 #37" in o.text, o.text
    assert [c for c in t37_tools["calls"] if c[0] == "t37_both"] == [], \
        "组合工具被拒却还是进了函数体"


def test_a_plain_approval_tool_still_runs_once_granted(t37_tools):
    """选项 b 不许误伤只声明需审批（不挂起）的工具：批准后照旧真执行。

    这是把"组合"这一刀精确切下去的正向对照：去掉组合判据本身没问题，去掉后这一条不该
    变；反过来把判据写成"凡 requires_approval 一律拒"，这一条红（execute_code 那种
    requires_approval、非 suspends 的正常闭环会被一起挡死）。道具用不挂起的 t37_calc。
    """
    from app.core import approval
    calls_seen = []
    info = tools_registry["t37_calc"]
    original = info["function"]

    def fake_calc(expression: str = "1+1"):
        calls_seen.append(expression)
        return 42

    info["function"] = fake_calc
    try:
        # 临时把这条**不挂起**的工具标成需审批——演的是 execute_code 那一族
        info["requires_approval"] = True
        req = approval.create_approval_request("t37_calc", {"expression": "1+1"},
                                               user_id="u-g")
        ticket = approval.consume_ticket(req["ticket_id"], "allow", "u-g")
        o = ex.execute_tool_detailed("t37_calc", {"expression": "1+1"}, user_id="u-g",
                                     granted_ticket=ticket)
        assert o.suspended is False, "不挂起的需审批工具不该交出挂起"
        assert o.ok is True, o.text
        assert o.tool_ran is True, "批准后没进函数体 = 选项 b 误伤了纯审批工具"
        assert calls_seen == ["1+1"]
    finally:
        info["function"] = original
        info["requires_approval"] = False


# ---------- 变异控制：新守卫的"负向控制"（没见过红的守卫=未测） ----------

def test_control_lying_tool_ran_would_burn_the_breakpoint(t37_tools, stub, monkeypatch):
    """⭐ 负向控制（工单 #37 主守卫的那把"如果谎报就红"的刀）。

    让执行器**谎报** tool_ran=True（即便频控在函数体之前就挡下、received 仍空），
    断点就会被烧——证明编排器"消费与否"这个决定**读的是这个来自世界的信号**，
    不是从 ok/success 猜。守卫（`if tool_ran: consume`）一旦被删成"无条件消费"或
    "只在 ok=True 时消费"，本文件其它用例（主判据 / 反向半边）就会红；这条把它们之间
    的那道因果显式钉住。
    """
    real = ex.execute_tool_detailed

    def lying(tool_name, arguments, user_id=None, granted_ticket=None, resume=None):
        o = real(tool_name, arguments, user_id=user_id, granted_ticket=granted_ticket,
                 resume=resume)
        return o._replace(tool_ran=True)          # 谎报：闸门其实挡了，却说"跑了"

    monkeypatch.setattr(orch_mod, "execute_tool_detailed", lying)
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    _fill_freq_gate("t37_gate", _UID)
    stub["task_boom"]["on"] = True

    orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")

    assert t37_tools["received"] == [], "控制前提：频控确实挡在函数体之前"
    assert task.resume_cursor.get("consumed") is True, (
        "谎报 tool_ran=True 却没烧断点 → 说明编排器根本没读这个信号，上面那条主判据是空的")


def test_control_neutered_trust_check_would_redeliver(t37_tools, stub, monkeypatch):
    """⭐ 控制 (b)（作者用过的同一类变异）：把"任何游标都可信"放开 → 信任那一族必须红。

    运行期把 `_trusted_failed_breakpoint` 换成"永远说可续"（不动一个字节源码），一枚
    **已被消费**的断点就会被第二次递进工具——外部副作用落两遍。这条证明六条不信任代码 +
    锁内复核不是摆设。构造是确定的：先用 task_boom 走一条**真的**"递进工具之后才坏"的路
    （received=1、consumed=True、failed），再把信任复核放开、带第二个答案进来——若这格
    判据还在（真代码），第二次会被 `consumed_breakpoint` 挡下；把它 monkeypatch 掉之后，
    第二个答案确实被第二次递进工具（received=2），正是放开判据会造成的那一次重复副作用。
    """
    orch = _orchestrator()
    task, _ = _park_through_the_breakpoint(orch)
    stub["task_boom"]["on"] = True      # 答案递进工具之后，第 3 步模型才炸 → failed + consumed
    first = orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")
    assert first["status"] == "failed", first
    assert len(t37_tools["received"]) == 1, "前提没成立：第一次没把答案递进工具"
    assert ts.get_task(task.task_id).resume_cursor.get("consumed") is True

    real = orch_mod._trusted_failed_breakpoint

    def always_trusted(t):
        susp = orch_mod._tool_suspension(t)
        if susp is None:
            return real(t)
        return susp, "resumable"        # 变异：consumed / 六种不信任一概不看

    monkeypatch.setattr(orch_mod, "_trusted_failed_breakpoint", always_trusted)
    orch2 = _orchestrator()
    orch2.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="9999")
    assert len(t37_tools["received"]) == 2, (
        "游标复核被放开后第二次答案确实被递进工具——这正是不放开时必须挡住的那一次副作用")


# ---------- ⚠️ 本机作业纪律：整个文件只写在自己的临时根里 ----------

def _paths_outside(pairs, allowed_roots):
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


def test_this_file_writes_only_inside_its_own_temp_root(t37_tools, stub, tmp_path):
    """跑完整条 #37 链路之后，每一个可变数据落点仍在临时根内；真库逐字节 + mtime 未动。"""
    orch = _orchestrator()
    before_bytes = REPO_DATA_TASKS.read_bytes()
    before_mtime = REPO_DATA_TASKS.stat().st_mtime_ns

    task, _ = _park_through_the_breakpoint(orch)
    _fill_freq_gate("t37_gate", _UID)
    stub["task_boom"]["on"] = True
    orch.run(goal=_GOAL, task_id=task.task_id, user_id=_UID, user_answer="8888")
    for tid in list(ts.task_store):
        ts.delete_task(tid)

    allowed = [str(tmp_path), tempfile.gettempdir()]
    offenders = _paths_outside([(label, path) for label, path in
                                __import__("app.core.paths", fromlist=["resolve_all_data_paths"])
                                .resolve_all_data_paths()], allowed)
    assert not offenders, f"可变数据落到了临时根之外：{offenders}"

    hostile = ("任务清单", os.path.realpath(str(REPO_DATA_TASKS)))
    assert hostile in _paths_outside([hostile], allowed), "尺子没牙：反例都没报出来"
    assert _paths_outside([("任务清单", os.environ["TASKS_DB_PATH"])], allowed) == []

    assert REPO_DATA_TASKS.read_bytes() == before_bytes, "真库的字节被改过"
    assert REPO_DATA_TASKS.stat().st_mtime_ns == before_mtime, "真库被原样重写过一次"
