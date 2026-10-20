"""v0.35 A3 行为契约：工具执行协议多出来的第三个出口——**停在断点问用户**。

规划提案 §二 第三行把这一格列为本版第三块地基：「执行器是同步函数，不是可中断状态机」，
证据写的是 `ToolResponse` 一次性返回；§四 v0.35 第三条要的是「工具执行协议从同步返回
`ToolResponse` 扩为『可挂起 → 等外部输入 → 恢复』」。判据落在那四条里的第 1 与第 4 条：

1. 跑到第 3 步需要验证码的任务能挂起、用户输入后**从第 4 步继续**、全上下文不丢
   （本文件把"5 分钟"那半条交给 A5 的超时语义与真机验收，见交付报告的未证实清单）；
2. 变异锁：删掉 `waiting_user` 那一跳 → 本文件的任务面测试红（判据 4）。

executor 的歧义是按提案自己的两个行号认门的：§四 v0.38 写「复用已下沉的执行器闸门
（`executor.py:169-176`）」与「`executor.py:21` 那个 10 次 / 600s 模型」——`tools/executor.py:21`
正是 `_FREQ_LIMITS = {"execute_code": (600.0, 10)}`、`:169-176` 正是那段 `requires_approval`
闸门；而 `agents/executor.py` 全文只有 39 行、既没有那把闸门也没有那本频控账，那份是
"用 TaskAgent 跑一个子任务"的薄壳。所以 A3 的协议改在 `tools/executor.py`。

一条贯穿全文件的纪律：**挂起绝不许被读成失败**。三条老读法各配一把负向锁——
① 按文本前缀猜成败（`startswith("✓")`），② 兜底 `except Exception` 把挂起折成
"工具调用解析错误/工具执行错误"，③ `guardrails` 的失败退避把两次挂起熔断成"这工具坏了"。

本票只铺协议 + **一条**消费路（TaskAgent → Executor → Orchestrator，因为只有这一条手里
有 Task、能把断点落成 `waiting_user`）。另外三条消费路（pipeline / streaming / react_agent）
与审批 confirm 端点今天仍会把挂起读成失败，逐条写在交付报告里，不在这里粉饰。

文件末尾那一族 `test_two_concurrent_answers_*` / `test_a_*_does_not_500_*` 是独立审查员
复现出来的**并发**那一格（两条答案同时回答同一个停点 → 前三步再烧一次钱 + 两个响应互相
矛盾；更窄的窗口里失败处理器自己抛的 `IllegalTaskTransition` 从端点飘出去 = 500）。它们
必须用 `httpx.ASGITransport` + 真并发驱动，理由与本仓那条硬约定同源（`TestClient` 每请求
另开一个 portal，跨请求互相影响的形状在它下面结构上测不到，见
`tests/test_event_loop_not_blocked.py` 文件头那次 524）。
"""
import asyncio
import copy
import inspect
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

import app.agents.orchestrator as orch_mod
import app.agents.task_agent as ta_mod
import app.tools.response as resp_mod
from app.agents import task_store as ts
from app.agents.orchestrator import TOOL_SUSPENSION_KIND, Orchestrator
from app.agents.task_agent import TaskAgent, TaskNeedsUser
from app.core import usage
from app.tools import executor as ex
from app.tools.registry import tools_registry
from app.tools.response import ToolResponse, ToolSuspended

_TAG = "tool_call"      # 本仓那条降级协议用的标记，这里拼出来而不是写第二份死字符串


def _call(name: str, arguments: dict) -> str:
    """模型在 TaskAgent 那条降级路上"要调工具"时的写法（与 task_agent 的判据同形）。"""
    return f"<{_TAG}>" + json.dumps({"name": name, "arguments": arguments},
                                    ensure_ascii=False) + f"</{_TAG}>"


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件；进出清空进程级存储（抄 A1/A2 那份夹具的理由）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture
def fake_tools():
    """这一票的道具箱：注册六条假工具，记两本账（跑过没跑过、真花钱的跑了几次）。

    `spent` 是本文件最重要的一本假账：**它代表"挂起之前那几步是不是真金白银"**。
    提案要的续跑必须是"从第 4 步接上"，所以任何一次续跑让 `spent` 里多出
    "step1-3" 就是重复付费——那几条测试就是数这个列表的长度。
    """
    spent: list = []
    calls: list = []

    def a3_order(item: str, resume: dict = None):
        calls.append(("a3_order", item, resume))
        if resume is None:
            # 第 1~3 步：查店、加购物车、发验证码。每一步都真花钱。
            spent.append("step1-3")
            raise ToolSuspended("请输入短信验证码",
                                {"step": 3, "item": item, "collected": {"shop": "甲"}})
        # 第 4 步起：拿着用户的答案接着跑，绝不回锅前三步
        assert isinstance(resume.get("state"), dict), "续跑没带断点"
        return (f"已提交订单（{resume['state']['item']}），"
                f"答案={resume['answer']}，接自第{resume['state']['step']}步")

    def a3_undeclared(qty: int = 1):
        calls.append(("a3_undeclared", qty, None))
        spent.append("step1-3")
        raise ToolSuspended("要继续吗", {"step": 1})

    def a3_silent():
        calls.append(("a3_silent", None, None))
        raise ToolSuspended("", {"step": 1})       # 挂起却问不出话

    def a3_split():
        calls.append(("a3_split", None, None))
        # 自相矛盾：同时说"跑完了"和"还在等人"
        return ToolResponse(True, data="跑完了", suspended=True,
                            prompt="请输入验证码", resume_state={"step": 2})

    def a3_uncarryable():
        calls.append(("a3_uncarryable", None, None))
        spent.append("step1-3")
        raise ToolSuspended("请输入收货地址", {"step": 4, "obj": object()})

    def a3_approved(note: str = "x", resume: dict = None):
        calls.append(("a3_approved", note, resume))
        raise ToolSuspended("请输入验证码", {"step": 2})

    def a3_calc(expression: str = "1+1"):
        calls.append(("a3_calc", expression, None))
        # 不写 eval：本仓的 calculator 早就因为 eval 修过一遍（见 agents/executor.py
        # 文件头那段），测试道具也不该把那个形状再带回来。
        return {"1+1": 2, "2*3": 6}.get(expression, 0)

    def a3_boom():
        calls.append(("a3_boom", None, None))
        raise RuntimeError("上游炸了")

    def a3_stateful_confirm(step_no: int = 1, resume: dict = None):
        """第二次挂起用的道具：接上一步之后还差一个答案。"""
        calls.append(("a3_stateful_confirm", step_no, resume))
        if resume is None:
            spent.append("step1-3")
            raise ToolSuspended("请输入短信验证码", {"step": 3, "need": "code"})
        if resume["answer"] == "8888":
            raise ToolSuspended("请输入收货地址", {"step": 5, "need": "address"})
        return f"两答案都收到了：{resume['answer']}"

    def a3_slowsleep(seconds: float = 0.01):
        """驱动失败退避用的道具：每次都失败。"""
        calls.append(("a3_slowsleep", seconds, None))
        spent.append("step1-3")
        raise RuntimeError("第 " + str(len(spent)) + " 次失败")

    entries = {
        "a3_order": {"function": a3_order, "description": "下一杯咖啡（要验证码）",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        "a3_undeclared": {"function": a3_undeclared, "description": "没声明可挂起",
                          "parameters": {"type": "object", "properties": {}},
                          "available": None, "needs_user": False,
                          "requires_approval": False, "suspends": False},
        "a3_silent": {"function": a3_silent, "description": "挂起却不问",
                      "parameters": {"type": "object", "properties": {}},
                      "available": None, "needs_user": False,
                      "requires_approval": False, "suspends": True},
        "a3_split": {"function": a3_split, "description": "自相矛盾",
                     "parameters": {"type": "object", "properties": {}},
                      "available": None, "needs_user": False,
                      "requires_approval": False, "suspends": True},
        "a3_uncarryable": {"function": a3_uncarryable, "description": "断点带不过去",
                           "parameters": {"type": "object", "properties": {}},
                           "available": None, "needs_user": False,
                           "requires_approval": False, "suspends": True},
        "a3_approved": {"function": a3_approved, "description": "要审批又可挂起",
                        "parameters": {"type": "object",
                                       "properties": {"note": {"type": "string"}}},
                        "available": None, "needs_user": False,
                        "requires_approval": True, "suspends": True},
        "a3_calc": {"function": a3_calc, "description": "算一下",
                    "parameters": {"type": "object",
                                   "properties": {"expression": {"type": "string"}}},
                    "available": None, "needs_user": False,
                    "requires_approval": False, "suspends": False},
        "a3_boom": {"function": a3_boom, "description": "坏掉的工具",
                    "parameters": {"type": "object", "properties": {}},
                    "available": None, "needs_user": False,
                    "requires_approval": False, "suspends": False},
        "a3_twice": {"function": a3_stateful_confirm, "description": "要两次答案",
                     "parameters": {"type": "object",
                                    "properties": {"step_no": {"type": "integer"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        "a3_fails": {"function": a3_slowsleep, "description": "一直失败的工具",
                     "parameters": {"type": "object", "properties": {}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    ledger_saved = dict(ex._FREQ_LEDGER)
    try:
        yield {"spent": spent, "calls": calls}
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous
        ex._FREQ_LEDGER.clear()
        ex._FREQ_LEDGER.update(ledger_saved)


# ---------- 三态本身：第三态不是失败态 ----------

def test_suspension_is_the_third_outcome_and_not_the_error_one(fake_tools):
    """挂起出来的 `ToolOutcome`：suspended=True、ok=False、文本是 `⏸` 那一支。

    三条断言各有各的红法：
    - 前缀写成 `✗`（或让 `_dispatch` 的兜底 except 接住）→ 第二条红；
    - 把 `ok` 改成 True → 第三条红（挂起不许冒充成功，模型会替用户把验证码编完）；
    - 忘了带 `suspended` → 第一条红。
    """
    outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u-1")
    assert outcome.suspended is True
    assert outcome.text.startswith("⏸ 需要用户输入：请输入短信验证码"), outcome.text
    assert not outcome.text.startswith("✗"), "挂起被折成了失败出口"
    assert outcome.ok is False, "挂起没产出结果，ok 不许说 True"
    # 问句必须原样送到模型眼前，否则它没法替用户问
    assert "请输入短信验证码" in outcome.text


def test_the_three_outcomes_have_three_mutually_distinct_text_projections(fake_tools):
    """只读文本的老消费者（`execute_tool` 那一条）也分得开三态。

    历史口径（这一族今天已经没有生产读者）：`task_agent.py:178` 早在 #76a 之前就改读
    `ok=bool(outcome.ok)`，`react_agent.py` 那句 `str(tool_result).startswith("✓")` 也在 #76b
    换成了 `outcome.ok`（现在写在 :212）。所以本条钉的是**文本形状本身**：三种前缀互不重叠，
    挂起那一支**既不是 ✓ 也不是 ✗**——按前缀猜成败的读法只会把它读成"这次没拿到可用结果"，
    那是安全的方向；读成"成功了"才是事故。
    """
    ok = ex.execute_tool("a3_calc", {"expression": "2*3"}, user_id="u-1")
    fail = ex.execute_tool("a3_boom", {}, user_id="u-1")
    wait = ex.execute_tool("a3_order", {"item": "美式"}, user_id="u-1")
    assert ok.startswith("✓ "), ok
    assert fail.startswith("✗ 错误: "), fail
    assert wait.startswith("⏸ "), wait
    prefixes = {ok[:1], fail[:1], wait[:1]}
    assert len(prefixes) == 3, f"三态前缀撞了：{prefixes}"


def test_the_pre_existing_two_outcomes_render_byte_identically(fake_tools):
    """向后兼容的正面证据：引入第三态**没有**改动前两态的任何字节。

    给模型的文本是全站契约（`tools/executor.py:104` 那句"签名与返回类型一律不动"）。
    这两行是从改动前的 `response.py:18-19` 逐字抄来的基准，不是从新代码的产物反推的。
    """
    assert ToolResponse(True, data=7).to_string() == "✓ 7"
    assert ToolResponse(False, error="炸了", hint="换个法子").to_string() == \
        "✗ 错误: 炸了。建议: 换个法子"
    # 老位置参数写法与老 ToolOutcome 五格写法都还得原样能用
    old = ToolResponse(True, "d", "e", "h", {"kind": "web_search"})
    assert old.artifacts == {"kind": "web_search"} and old.suspended is False
    five = ex.ToolOutcome("t", True, None, False, 5)
    assert (five.text, five.ok, five.suspended, five.suspension) == ("t", True, False, None)


def test_suspension_carries_enough_state_to_continue_later(fake_tools):
    """提案要的那半句"with enough state carried"：一包能接回去的断点，四样都不能少。

    `arguments` 存的是**服务端这次调用真正用的那一份**（与审批那条同一条纪律：
    不重新采纳事后回传的东西）；`resume_state` 必须过得了 JSON 往返，因为它随后
    要进 `data/tasks.json`。
    """
    outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u-1")
    suspension = outcome.suspension
    assert set(suspension) == {"tool_name", "arguments", "prompt",
                               "resume_state", "can_resume"}
    assert suspension["tool_name"] == "a3_order"
    assert suspension["arguments"] == {"item": "拿铁"}
    assert suspension["prompt"] == "请输入短信验证码"
    assert suspension["can_resume"] is True
    # 断点必须是一张能落盘、能读回来的表（跨重启那一半由 A1 的 resume_cursor 承担）
    assert json.loads(json.dumps(suspension["resume_state"])) == suspension["resume_state"]
    assert suspension["resume_state"]["step"] == 3


def test_no_suspension_signal_ever_escapes_the_executor(fake_tools):
    """挂起在协议里是**值**，不是异常：两条公开函数都只回 str / ToolOutcome。

    这条是向后兼容的承重墙。调用侧全都写着兜底 `except Exception`
    （`streaming.py:553`、`pipeline.py`、`react_agent.py:215`），异常一旦外飘立刻被
    折成一句"工具执行错误"，第三态当场退化回失败态。
    """
    try:
        text = ex.execute_tool("a3_order", {"item": "拿铁"}, user_id="u-1")
        detail = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u-1")
    except Exception as e:                          # noqa: BLE001 - 就是要抓这个
        pytest.fail(f"挂起信号飘出执行器了，老调用侧会把它读成失败：{type(e).__name__}: {e}")
    assert isinstance(text, str) and isinstance(detail, ex.ToolOutcome)


# ---------- fail-closed：判不出是等还是完，就不许自动继续 ----------

def test_a_suspension_without_a_question_becomes_an_error(fake_tools):
    """没问句的挂起按失败处理：没人能接上这一步，所以不假装"还在等"。"""
    outcome = ex.execute_tool_detailed("a3_silent", {}, user_id="u-1")
    assert outcome.suspended is False, "问不出话的挂起不该说自己在等人"
    assert outcome.ok is False
    assert outcome.suspension is None
    assert outcome.text.startswith("✗ 错误: "), outcome.text


def test_a_contradictory_outcome_is_never_read_as_success(fake_tools):
    """`success=True` 与 `suspended=True` 同时出现 = 协议判不出这一次到底跑完没有。

    两个下场都危险：当挂起 → 用户等不到结果；当成功 → 模型以为办完了继续往下编。
    所以第三选一：明说失败、既不自动继续也不替你假设结果。
    """
    outcome = ex.execute_tool_detailed("a3_split", {}, user_id="u-1")
    assert outcome.suspended is False and outcome.ok is False
    assert "无法判断" in outcome.text or "按失败处理" in outcome.text, outcome.text


def test_a_suspension_whose_state_cannot_be_carried_promises_no_resume(fake_tools):
    """断点带不过去（这里是一张装着 object() 的 dict）→ 仍然挂起，但 `can_resume=False`。

    为什么 can_resume 要在这里判死而不是等落盘再炸：A1 那份存储的不变式是
    `can_resume ⟺ status 是 waiting_user`（task_store.py:190 与 :263 同一判据）。
    没有这一格，编排器就会把接不回去的任务写成 waiting_user（见
    `test_a_suspension_nobody_can_carry_is_parked_as_failed_not_waiting`）。
    """
    outcome = ex.execute_tool_detailed("a3_uncarryable", {}, user_id="u-1")
    assert outcome.suspended is True
    assert outcome.suspension["can_resume"] is False
    assert outcome.suspension["prompt"] == "请输入收货地址"


def test_resume_without_an_answer_is_refused_and_runs_absolutely_nothing(fake_tools):
    """⭐ 自动继续那扇门必须锁死：没有答案 = 不续跑，且工具一次都不许被执行。

    这是本票最贵的一条：一次没有答案的续跑等于让工具替用户编一个验证码往下跑，
    而且把前三步的真钱再烧一遍。负向锁：把 `executor._resume_refusal` 里那句
    `answer` 判空删掉，`calls` 立刻非空、`spent` 立刻多一笔 → 这条红。
    """
    state = {"step": 3, "item": "拿铁", "collected": {"shop": "甲"}}
    for bad in ({"state": state, "answer": "   "}, {"state": state},
                {"state": state, "answer": None}):
        fake_tools["calls"].clear()
        fake_tools["spent"].clear()
        outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"},
                                           user_id="u-1", resume=bad)
        assert outcome.suspended is False, f"缺答案的续跑被放行了：{bad}"
        assert outcome.ok is False
        assert fake_tools["calls"] == [], "续跑被拒却仍然执行了工具函数"
        assert fake_tools["spent"] == [], "拒掉的续跑还是把钱花了"


def test_resume_with_the_wrong_state_shape_is_refused(fake_tools):
    """断点形状不对（None / 字符串 / 别的类型）一律不接：宁可不动，也不从头再跑。"""
    for bad_state in (None, "3", ["step"], 7):
        outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u-1",
                                          resume={"state": bad_state, "answer": "8888"})
        assert outcome.ok is False and outcome.suspended is False
    assert fake_tools["calls"] == []


def test_resume_is_refused_for_a_tool_that_never_declared_itself_suspends(fake_tools):
    """没在注册表里声明 `suspends` 的工具，执行器不往里递断点。

    为什么是声明而不是看函数签名猜：与 `needs_user` 同一条纪律——能不能把服务端
    存着的东西递进函数，是工具的契约。靠 inspect 猜"它有个叫 resume 的参数"，
    一次改名就悄悄改了协议行为。
    """
    outcome = ex.execute_tool_detailed("a3_undeclared", {}, user_id="u-1",
                                       resume={"state": {"step": 1}, "answer": "要"})
    assert outcome.ok is False
    assert "suspends" in outcome.text, outcome.text
    # 但第一次调用仍然照旧被认成挂起（只是没人接得住）
    first = ex.execute_tool_detailed("a3_undeclared", {}, user_id="u-1")
    assert first.suspended is True and first.suspension["can_resume"] is False


def test_resume_still_pays_the_approval_gate(fake_tools):
    """续跑**不放宽**审批：需要审批的工具没有票据，第二次进来照样拒执行。

    这条既是 fail-closed 也是本票的已知边界：现网任务面（TaskAgent/Orchestrator）
    从不带 granted_ticket，所以"要审批 + 会挂起"的工具今天接不回去——要补得先让
    票据能跟着断点跨请求走，那要动 `approval.py`（本票不许动），已上报 follow-up。
    """
    fake_tools["calls"].clear()
    first = ex.execute_tool_detailed("a3_approved", {"note": "n"}, user_id="u-1")
    assert first.ok is False and first.suspended is False, "未批准的调用绝不该跑到工具里"
    assert fake_tools["calls"] == []
    resume = ex.execute_tool_detailed("a3_approved", {"note": "n"}, user_id="u-1",
                                      resume={"state": {"step": 2}, "answer": "8888"})
    assert resume.ok is False and resume.suspended is False
    assert fake_tools["calls"] == [], "续跑绕开了审批闸门"


def test_resume_still_pays_the_frequency_gate(fake_tools):
    """续跑**不放宽**频控：那一本 10 次 / 600 秒的账照旧计数（`tools/executor.py:21`）。

    做法：给 `a3_order` 临时挂上一条频控规则（`_FREQ_LIMITS` 是这张表，判据在
    `tools/executor.py:21` 与 `_freq_gate`），先把这个人的账本灌满，再看续跑能不能
    白拿一次执行。负向锁：把 `_freq_gate` 那句调用删掉，
    `fake_tools["calls"]` 会非空 → 这条红。
    """
    saved_limit = ex._FREQ_LIMITS.get("a3_order")
    ex._FREQ_LIMITS["a3_order"] = (600.0, 10)
    try:
        with ex._FREQ_LOCK:
            ex._FREQ_LEDGER[("a3_order", "u_freq")] = [time.monotonic()] * 10
        outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"},
                                           user_id="u_freq",
                                           resume={"state": {"step": 3, "item": "拿铁"},
                                                 "answer": "8888"})
        assert outcome.ok is False and outcome.suspended is False
        assert "频繁" in outcome.text, outcome.text
        assert fake_tools["calls"] == [], "续跑白拿了一次被频控挡住的执行"
    finally:
        if saved_limit is None:
            ex._FREQ_LIMITS.pop("a3_order", None)
        else:
            ex._FREQ_LIMITS["a3_order"] = saved_limit


# ---------- 钱与预算：挂起不双花、也不把人已花的抹掉 ----------

def test_resume_continues_at_the_breakpoint_and_never_re_pays_steps_1_to_3(fake_tools):
    """提案 §四 判据 1 的钱那一半：第 4 步接上，前三步**一次都不许多跑**。

    这是整张票最硬的一条断言，因为"挂起 + 续跑"最坏的实现就是"从头再跑一遍"：
    症状不是报错，是账单翻倍 + 重复下单。负向锁：删掉 `_dispatch` 里
    `kwargs["resume"] = {...}` 那一行注入，第二次调用拿不到断点 → 从头再跑 →
    `spent` 出现两笔 → 这条红。
    """
    first = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u-1")
    assert first.suspended is True
    assert fake_tools["spent"] == ["step1-3"], "挂起这一次已经花过钱了"

    suspension = first.suspension
    second = ex.execute_tool_detailed(
        suspension["tool_name"], suspension["arguments"], user_id="u-1",
        resume={"state": suspension["resume_state"], "answer": "8888"})

    assert second.suspended is False and second.ok is True, second.text
    assert "8888" in second.text and "接自第3步" in second.text, second.text
    assert fake_tools["spent"] == ["step1-3"], (
        f"续跑把已经付过钱的步骤又跑了一遍：{fake_tools['spent']}")


def test_a_refused_resume_consumes_no_frequency_slot(fake_tools):
    """被判据挡下的续跑**不占**那一本频控账，也不许跑到工具里。

    方向要说清：频控闸在 `_dispatch` 里排在续跑判据**之后**，为的是"根本没开始的
    调用不该消耗这个人的额度"——与本仓既有那句"被闸门挡下的调用不该已经拉起过容器"
    （`tools/executor.py:190`）同一个理由。

    道具刻意用 `a3_order` 而不是 `execute_code`：**第一版这条是假绿**——它拿
    execute_code 做道具，而 execute_code 挂着审批闸（`requires_approval=True`），
    那一支在续跑判据之前就 return 了，于是"删掉 `_resume_refusal`"（M2）与
    "把频控挪到续跑判据之前"（M9）两条变异都红不了它。换成没有审批闸、只吃频控的
    道具之后，M2 与 M9 都让它红（见交付报告的变异台账）。
    """
    saved_limit = ex._FREQ_LIMITS.get("a3_order")
    ex._FREQ_LIMITS["a3_order"] = (600.0, 10)
    try:
        outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u_slot",
                                           resume={"state": None, "answer": ""})
        assert outcome.ok is False and outcome.suspended is False
        assert fake_tools["calls"] == [], "被拒的续跑还是跑到工具里去了"
        assert ("a3_order", "u_slot") not in ex._FREQ_LEDGER, \
            "被拒的续跑还占了一次频控计数位"
    finally:
        if saved_limit is None:
            ex._FREQ_LIMITS.pop("a3_order", None)
        else:
            ex._FREQ_LIMITS["a3_order"] = saved_limit


def test_the_suspend_text_carries_no_success_mark(fake_tools):
    """挂起的文本里不许出现 `✓`：老读法（`startswith("✓")`）读到它也不能当成办成了。"""
    outcome = ex.execute_tool_detailed("a3_order", {"item": "拿铁"}, user_id="u-1")
    assert "✓" not in outcome.text, outcome.text
    assert outcome.text.startswith("⏸ ")


def test_suspending_round_is_counted_and_not_credited_as_success(fake_tools, monkeypatch):
    """挂起落在护栏账上：**记这一轮**（轮次是真花掉的），但**不占**本轮输出预算。

    三个反面都要防：
    - 不 `record_outcome` → 本轮轮次少记一笔 → 成本上限被绕过（本仓那条
      "尝试即计数"的口径，见 tools/executor.py:16-18）。
    - 先 `clip_output` 再抛 → 那句根本没给模型看的问话还吃掉了本轮输出预算。
    - 挂起被记成失败 → 退避/熔断那一族事件混进这一轮的账里。
    负向锁：删掉 `task_agent.py` 里 `guard.record_outcome(tool_name, ok=..., suspended=...)`
    那一行 → `tool_rounds == 1` 立刻红。

    ⭐ 下面最后那组断言在工单 #76a 改过一次，改前/改后各钉什么要说清（不写就变成
    "谁红改谁"）：#29 只开了护栏侧的入口，调用点还传 `ok=bool(outcome.ok)`，挂起被读成
    **失败**，那一轮一条事件都不会产生 → 这里钉的是 `types == []`。#76a 把 task_agent
    接上 `suspended=` 之后，`guardrails.py:171-172` 必然追加一条 `suspend` 事件 →
    空清单那条断言必红（本单最大的红点）。现在钉的是"**只有** `suspend` 这一条、而且是
    机器自己数出来的那一条"：类型清单、工具名、`suspension_counts` 三样同时成立，并且
    熔断/退避那一族（fail_backoff / duplicate / cost_cap_*）一条都不许出现。
    于是红的是这两种回归：① 摘掉 `suspended=`（挂起顺流成失败）→ 清单里冒出
    `fail_backoff`、`suspension_counts` 变空 → 红；② 把挂起干脆不记账 → `tool_rounds`
    那句先红。**不许**放宽成 `>= 0` 或 `in [...]`：那等于把"挂起照记一格账"这件事又放回隐形。
    """
    seen = []
    from app.core.guardrails import RoundGuard as RealGuard

    def spy_guard(*args, **kwargs):
        g = RealGuard(*args, **kwargs)
        seen.append(g)
        return g

    monkeypatch.setattr("app.agents.task_agent.RoundGuard", spy_guard)
    monkeypatch.setattr("app.agents.task_agent.get_llm_response",
                        lambda *a, **k: _call("a3_order", {"item": "拿铁"}))
    agent = TaskAgent(name="T", model="m", tools_schema=[], tools=tools_registry,
                      user_id="u-1")
    with pytest.raises(TaskNeedsUser) as caught:
        agent.run("点一杯咖啡")
    assert caught.value.can_resume is True
    assert len(seen) == 1
    guard = seen[0]
    assert guard.tool_rounds == 1, "挂起那一轮没记账，等于给重试留了免费额度"
    assert guard.output_chars == 0, "没给模型看的问话占用了本轮输出预算"
    types = [e["type"] for e in guard.guard_events]
    assert types == ["suspend"], f"挂起这一轮只许有一条 suspend：{guard.guard_events}"
    assert guard.guard_events[0]["tool"] == "a3_order", guard.guard_events
    assert guard.guard_events[0]["suspensions"] == 1, guard.guard_events
    assert guard.suspension_counts == {"a3_order": 1}, guard.suspension_counts
    for forbidden in ("fail_backoff", "duplicate", "cost_cap_rounds", "cost_cap_tokens"):
        assert forbidden not in types, f"挂起这一轮冒出熔断/退避类事件 {forbidden}：{types}"


def test_usage_already_spent_before_a_suspension_stays_on_the_ledger(fake_tools, monkeypatch):
    """挂起不改账本：已经发生的每次模型调用各记一次，一次都不许少、一次都不许多。

    形状上的根据是这条路上钱记在**每一次模型调用**的当场（`llm_client.py:66`），
    而挂起是值不是异常（见 `test_no_suspension_signal_ever_escapes_the_executor`），
    所以没有任何 `finally` / 记账路径会被跳过。负向锁：把工具改成**抛普通异常**，
    这条不变（异常也一样记过账），但把 `get_llm_response` 的调用挪到挂起之后
    再补一次 → records 变 2 → 这条红。
    """
    records = []
    monkeypatch.setattr(usage, "record_call",
                        lambda **kw: records.append(kw))
    responses = [_call("a3_order", {"item": "拿铁"})]

    def fake_llm(model, messages, **kw):
        # 与 llm_client 同一个口径：调用当场就记一次账
        usage.record_call(user_id="u-1", provider_id="p", paid_by="user",
                          prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return responses.pop(0)

    monkeypatch.setattr("app.agents.task_agent.get_llm_response", fake_llm)
    agent = TaskAgent(name="T", model="m", tools_schema=[], tools=tools_registry,
                      user_id="u-1")
    with pytest.raises(TaskNeedsUser):
        agent.run("点一杯咖啡")
    assert len(records) == 1, f"挂起把已经发生的消耗记成了 {len(records)} 笔"
    assert records[0]["total_tokens"] == 15


# ---------- 任务面：挂得起、也接得回（判据 1 与判据 4） ----------

def _orchestrator_with(monkeypatch, tool_call_names=("a3_order",)):
    """装一台不叫真模型的编排器：规划与汇总两条模型路都换成桩。"""
    orch = Orchestrator(model="m")
    monkeypatch.setattr("app.agents.orchestrator.get_llm_response",
                        lambda *a, **k: "最终汇总")
    state = {"answers": list(tool_call_names), "seeded": []}

    def fake_llm(model, messages, **kw):
        # 只要对话里还没有"工具执行结果"，就说要调工具；有了就给最终答案
        already = any(str(m.get("content", "")).startswith("工具执行结果")
                      for m in messages)
        if already:
            return "拿到了，继续"
        name = state["answers"].pop(0) if state["answers"] else "a3_calc"
        return _call(name, {"item": "拿铁"} if name == "a3_order" else
                     {"step_no": 1} if name == "a3_twice" else {})

    monkeypatch.setattr("app.agents.task_agent.get_llm_response", fake_llm)
    return orch, state


def _running_task(user_id="u-1", subtasks=("选店", "下单")):
    task = ts.Task(goal="点一杯咖啡", subtasks=list(subtasks), user_id=user_id)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    return task


def test_a_suspension_parks_the_task_as_waiting_user_with_the_tool_breakpoint(
        fake_tools, isolated_tasks, monkeypatch):
    """TaskAgent → Executor → Orchestrator 这条链把挂起落成一等状态。

    提案 §四 判据 4 要的变异锁就落在这里：`ALLOWED_TRANSITIONS[RUNNING]` 里删掉
    `WAITING_USER`（或把 task_agent 那两行 `except TaskNeedsUser: raise` 删掉），
    这一条与下面那条一起红。
    """
    orch, _ = _orchestrator_with(monkeypatch)
    task = _running_task()
    res = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")
    assert res["status"] == "waiting_user", res
    assert res["question"] == "请输入短信验证码"
    assert res["can_resume"] is True
    assert task.status is ts.TaskStatus.WAITING_USER
    assert task.can_resume is True
    cursor = task.resume_cursor
    assert cursor["kind"] == TOOL_SUSPENSION_KIND
    assert cursor["tool_name"] == "a3_order" and cursor["arguments"] == {"item": "拿铁"}
    assert cursor["resume_state"]["step"] == 3
    assert cursor["question"] == "请输入短信验证码"    # A1 读侧认的就是这一格
    assert cursor["subtask_index"] == 0
    # 挂起不是失败：error 那一格必须是空的
    assert task.error is None
    # 也没重复付费
    assert fake_tools["spent"] == ["step1-3"]


def test_parked_suspension_survives_a_restart_and_the_answer_resumes_it_from_step_4(
        fake_tools, isolated_tasks, monkeypatch):
    """判据 1 + 判据 2 合起来：挂起 → 进程重启 → 带答案回来 → 从第 4 步跑完，全程不重付。

    `ts.restore()` 就是本仓认定"重启过一次进程"的做法（A1/A2 同款）。
    """
    orch, _ = _orchestrator_with(monkeypatch)
    task = _running_task()
    first = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")
    assert first["status"] == "waiting_user"

    ts.restore(path=str(isolated_tasks))
    back = ts.get_task(task.task_id)
    assert back is not None, "挂起的任务没落盘：重启即丢"
    assert back.status is ts.TaskStatus.WAITING_USER and back.can_resume is True
    assert back.interrupted_by_restart is False, "等人不等于被重启打断"

    res = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1",
                   user_answer="8888")
    assert res["status"] == "completed", res
    assert fake_tools["spent"] == ["step1-3"], f"续跑重付了前三步：{fake_tools['spent']}"
    # 续跑时递进工具的那一包，必须就是重启前落盘的断点 + 用户刚给的答案
    resumed = [c for c in fake_tools["calls"] if c[2] is not None]
    assert len(resumed) == 1 and resumed[0][2]["answer"] == "8888"
    assert resumed[0][2]["state"]["step"] == 3
    # 答案本身不落盘（里面可能是验证码），落盘的只有断点
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))
    assert "8888" not in json.dumps(on_disk, ensure_ascii=False)


def test_resuming_without_an_answer_touches_nothing_and_asks_again(
        fake_tools, isolated_tasks, monkeypatch):
    """⭐ 没有答案就再调一次本端点：状态一格不改、工具一次不跑、钱一分不花。

    这条同时钉三个反面：
    - 不许"往下走把整条子任务从头再跑一遍"（老写法就是这么做的，那是再烧一次钱）；
    - 不许假跳 RUNNING（那会破坏 A1 的 `can_resume ⟺ waiting_user` 不变式）；
    - 不许把挂着的问题吞掉。
    负向锁：把 orchestrator 那句 `if not answer: return {...}` 删掉 →
    `spent` 多出钱、状态被改成 RUNNING → 这条红。
    """
    orch, _ = _orchestrator_with(monkeypatch)
    task = _running_task()
    orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")
    before = copy.deepcopy(task.to_dict())
    spent_before = list(fake_tools["spent"])
    calls_before = list(fake_tools["calls"])

    res = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")

    assert res["status"] == "waiting_user", res
    assert res["question"] == "请输入短信验证码"
    assert ts.get_task(task.task_id).to_dict() == before, "没有答案的那次改动过任务"
    assert fake_tools["spent"] == spent_before, "没有答案还把钱花了"
    assert fake_tools["calls"] == calls_before, "没有答案却把工具又跑了一次"


def test_a_second_suspension_replaces_the_breakpoint_without_losing_the_chain(
        fake_tools, isolated_tasks, monkeypatch):
    """接上一步之后还差一个答案（先验证码、再收货地址）：再挂一次，断点换成新的。

    这条走的是 `waiting_user → running → waiting_user` 那一圈跳转表——A1 的
    `test_wait_for_user_twice_overwrites_the_cursor` 讲的是同一条形状，本条讲的是
    **工具真的被接回去过**（`spent` 只有一笔）而第二次挂起用的是新断点。
    """
    orch, _ = _orchestrator_with(monkeypatch, tool_call_names=("a3_twice",))
    task = _running_task()
    first = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")
    assert first["question"] == "请输入短信验证码"

    res = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1",
                   user_answer="8888")
    assert res["status"] == "waiting_user", res
    assert res["question"] == "请输入收货地址", res
    cursor = ts.get_task(task.task_id).resume_cursor
    assert cursor["resume_state"] == {"step": 5, "need": "address"}
    assert "8888" not in json.dumps(cursor, ensure_ascii=False)
    assert fake_tools["spent"] == ["step1-3"]


def test_a_suspension_nobody_can_carry_is_parked_as_failed_not_waiting(
        fake_tools, isolated_tasks, monkeypatch):
    """接不回去的挂起（`can_resume=False`）如实写 failed，绝不写 waiting_user。

    为什么这算一条独立判据而不是洁癖：把接不住的任务写成"在等人"，就是让
    GET /v1/tasks 对用户许诺"给个答案就能续"，而那个答案递进去只会被执行器拒掉——
    任务永远挂在里面，谁也说不清它在等谁。A1 的不变式 `can_resume ⟺ waiting_user`
    在这一格必须站得住。
    负向锁：把 `_park_suspension` 里 `if not suspension.get("can_resume")` 那一支删掉，
    这一条红（状态会变成 waiting_user）。
    """
    orch, _ = _orchestrator_with(monkeypatch, tool_call_names=("a3_uncarryable",))
    task = _running_task()
    res = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")
    assert res["status"] == "failed", res
    assert task.status is ts.TaskStatus.FAILED
    assert task.can_resume is False and task.waiting_since is None
    assert "请输入收货地址" in task.error          # 问题原样留着，供人工接着办
    assert res["can_resume"] is False


def test_the_waiting_user_transition_is_what_parks_it(fake_tools, isolated_tasks,
                                                      monkeypatch):
    """提案 §四 判据 4（变异锁）在 A3 这条路上的形状：那张跳转表就是承重墙。

    这里用 `monkeypatch.setitem` 在**运行期**把 `ALLOWED_TRANSITIONS[RUNNING]` 里
    那一格抹掉（不改 task_store.py 一个字节）：挂起必须炸，而不是悄悄落进别的状态。
    然后按既有编排器语义收在 failed —— 也就是"挂不起来就明说没做成"，绝不假装在等人。
    """
    neutered = frozenset({ts.TaskStatus.RUNNING, ts.TaskStatus.COMPLETED,
                          ts.TaskStatus.FAILED, ts.TaskStatus.CANCELLED})
    monkeypatch.setitem(ts.ALLOWED_TRANSITIONS, ts.TaskStatus.RUNNING, neutered)
    assert ts.can_transition("running", "waiting_user") is False

    orch, _ = _orchestrator_with(monkeypatch)
    task = _running_task()
    res = orch.run(goal="点一杯咖啡", task_id=task.task_id, user_id="u-1")
    assert res["status"] != "waiting_user", "抹掉跳转之后还挂得起来，说明这条锁没牙"
    assert task.status is ts.TaskStatus.FAILED


# ---------- 向后兼容与本机作业纪律 ----------

def test_no_new_sse_frame_shape_for_old_clients():
    """过程帧的字节形状一个比特都没动：老客户端**看不到**新帧，也绝不炸。

    这一条说的是"老客户端看见什么"：A3 只加协议与任务面，SSE 那一帧（waiting_user）
    是 A4 的票面（见 A1 测试文件开头同一条 defer）。所以老客户端今天看到的挂起是
    一次 `ok:false` 的 tool_result 帧 + 那句 `⏸ 需要用户输入：…` 的 summary——
    读成"这一步没成"，永远读成"办成了"是不会发生的。
    """
    from app.core import stream_events as se
    frame = se.tool_result_frame("c_1", "a3_order", False, "⏸ 需要用户输入：x",
                                 elapsed_ms=12, truncated=False)
    assert set(frame) == {"type", "id", "name", "ok", "summary", "elapsed_ms",
                          "truncated"}
    assert se.FRAME_TOOL_RESULT == "tool_result"
    assert "waiting_user" not in se.CLIENT_FRAMES, "SSE 那一帧属于 A4，不属于这一票"


def test_the_protocol_gained_no_new_status_literal():
    """任务状态字面量仍然只有那六个；第三态在协议里是**字段**，不是第二个状态。

    与本仓那条纪律同源：`resumable` / `suspended` 这两个词各有别的含义
    （`stream_runs.py:248` 的函数 `resumable()` 与 JSON 布尔 `resumable`、壳侧
    `Api.kt` 那个不是一回事的 Boolean），再拿它们当状态名等于让下一个人猜是哪一面。
    所以 A3 全程只说布尔字段（suspended / can_resume），往外报的 status 值一律取自
    既有的 `TaskStatus`。

    判据用 AST 扫 `"status"` 键上的常量。第一版是拿字符串字面量全文扫的，被
    `getattr(response, "suspended")` 这种**字段名**误报红过一次——那是测试的错不是
    代码的错，扫法要扫"报出去的状态值"，不是扫"出现过的英文单词"。反例同场跑一遍：
    一段真写着 status 为 suspended 的源码必须被报出来，否则这条绿没牙。
    """
    import ast

    known = {m.value for m in ts.TaskStatus}
    assert sorted(known) == ["cancelled", "completed", "failed", "pending", "running",
                             "waiting_user"], "TaskStatus 冒出了第七个成员"

    def status_literals(source):
        found = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "status"
                            and isinstance(value, ast.Constant)):
                        found.append(value.value)
        return found

    used = set(status_literals(inspect.getsource(orch_mod)))
    assert used <= known, f"编排器报出了表外的状态值：{sorted(used - known)}"
    assert "waiting_user" in used, "扫不到 waiting_user，说明这条判据根本没看到新出口"
    assert status_literals('d = {"status": "suspended"}') == ["suspended"], \
        "反例都没报出来，上面那条绿不算证据"
    # 第三态在协议里是字段：这两个新格必须是 ToolOutcome 的尾巴，不是第二个状态
    assert ex.ToolOutcome._fields[-2:] == ("suspended", "suspension")
    assert ToolResponse(True).suspended is False
    assert ex.ToolOutcome("t", True, None, False, 5).suspended is False



def test_no_new_background_threads_timers_or_loop_blocking_io():
    """⚠️ 本机作业纪律：这一票不起线程、不起定时器、不在事件循环里加阻塞 I/O。

    判据是 AST 扫三条改动文件里的 `Thread(` / `Timer(` / `create_task` /
    `run_in_executor`；扫描器自己配**反例**跑一遍（拿一段真含 threading.Thread 的
    源码喂它，必须报出来），否则这条就是恒绿的假锁——本仓那种"验自己"的假绿吃过账。

    落盘那一格为什么合法：断点写进 `data/tasks.json` 走的是 A1 已有的
    `Task.wait_for_user → save → core/atomic_write`，调用点是**同步 def** 的
    `/v1/agent/orchestrate`（FastAPI 把整个处理函数丢线程池），这条判据由
    `tests/test_event_loop_not_blocked.py` 钉着；同一条路上改动前就已经在写这份文件
    （`orchestrator.py` 的 `task_store[task.task_id] = task` 与 `set_status`）。
    """
    banned = ("Thread(", "Timer(", "create_task", "run_in_executor",
              "asyncio.", "schedutil")
    targets = {
        "app/tools/executor.py": inspect.getsource(ex),
        "app/tools/response.py": inspect.getsource(resp_mod),
        "app/agents/task_agent.py": inspect.getsource(ta_mod),
        "app/agents/orchestrator.py": inspect.getsource(orch_mod),
    }
    for name, source in targets.items():
        hits = [b for b in banned if b in source]
        assert not hits, f"{name} 里冒出了常驻线程/定时器/线程池调用：{hits}"
    # 反例：同一个判据对真含线程的源码必须报红，否则上面那段绿是没牙的
    control = "def f():\n    threading.Thread(target=g).start()\n"
    assert any(b in control for b in banned), "扫描器没牙：反例都没报出来"
    # 事件循环那一侧的既有形状：编排端点必须是同步 def（不是这一票改的，钉一下）
    import inspect as _i
    from app.main import orchestrate_task
    assert not _i.iscoroutinefunction(orchestrate_task), \
        "/v1/agent/orchestrate 变成 async 就会把落盘摁在事件循环上"


def test_threading_lock_is_still_the_only_concurrency_primitive_in_the_executor():
    """正控 + 反例两跑的 AST 锁：executor 里只许有既有那把 `threading.Lock`。

    反例走 `ast.parse` 现造的一段代码，不改任何源文件。
    """
    import ast
    tree = ast.parse(inspect.getsource(ex))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(ast.unparse(node.func))
    assert "threading.Lock" in calls, "频控那把锁没了（本票的既有形状）"
    forbidden = {"threading.Thread", "threading.Timer", "concurrent.futures.ThreadPoolExecutor",
                 "asyncio.create_task", "anyio.from_thread.start_blocking_portal"}
    assert not (set(calls) & forbidden), f"执行器里冒出了新的并发原语：{set(calls) & forbidden}"
    control = ast.parse("threading.Timer(1, f).start()")
    found = {ast.unparse(n.func) for n in ast.walk(control) if isinstance(n, ast.Call)}
    assert found & forbidden, "反例都没报出来，上面那条绿不算证据"


# ---------- ⭐ 并发那一格：同一个停点只许一个人接（独立审查员复现出来的形状） ----------
#
# 审查员在本机拿两条真并发请求复现出来的这一格（v0.35 A4 接第一个真挂起工具之前必须
# 修掉的）：
#
#   [1] status=waiting_user http=200 question=请输入短信验证码 can_resume=True
#   [2] http=200 status=waiting_user   [3] http=200 status=completed
#   TOOL CALLS total=2 resumed=1 fresh(=重跑第 1~3 步)=1 → 前三步再烧一次 = True
#
# 机制在 `orchestrator.py`：第一条把任务从 waiting_user 翻成 RUNNING 之后，第二条就
# **看不见 waiting_user** 了，于是掉进那句通用的"恢复任务 … 进度: 0/1"，把整条子任务
# 从第 1 步重跑——那正是 A3 承诺省掉的那一笔钱（重复下单 + 重复发验证码）；两个响应还
# 互相矛盾（一个说还在等人，一个说办完了）。更窄的窗口里审查员另外拿到一枚**从端点
# 飘出去**的 `IllegalTaskTransition: completed → failed`，抛点就在失败处理器里面
# （`orchestrator.py:318` 那一跳）→ 客户端看到的是 500。
#
# 这一族测试必须用 `httpx.ASGITransport` + 真并发驱动，理由与本仓那条硬约定同源：
# `TestClient` 每个请求另开一个 portal（各自一条循环、各自一套线程池），跨请求互相
# 影响的形状在它下面结构上测不到（判据原文见 `tests/test_event_loop_not_blocked.py`
# 文件头那次线上 524）。会合点全在 `threading.Event` / `threading.Barrier` 上，
# **不靠 sleep 抢时机**：每个会合点还配一句"前提没成立"的断言，防止它变成空测。

_GOAL = "点一杯咖啡"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"


@pytest.fixture
def race_tools():
    """并发这一族用的道具箱。两本假账都记在**闭包**里（抄 `fake_tools` 的记法）。

    为什么必须记在闭包：判据问的是"这个世界里第 1~3 步被付了几次"，不是"被测对象自己
    申报花了多少"。拿 `task.results` 之类的字段去证明没重复付费，等于让被告自己开证明——
    而 `task` 恰好就是那一个被两条请求同时改写的对象。
    """
    spent: list = []        # 每一笔 = 一次真跑了第 1~3 步（查店 / 加购 / 发验证码）
    resumed: list = []      # 每一次"从断点接上"时递进工具的整包
    llm: list = []          # 模型调用（同一种钱，另一本账）
    inside_breakpoint = threading.Event()
    release = threading.Event()
    hold = {"on": True}     # 断点那一步要不要停在会合点上

    def a3r_gate(item: str = "拿铁", resume: dict = None):
        if resume is None:
            spent.append("step1-3")
            raise ToolSuspended("请输入短信验证码",
                                {"step": 3, "item": item, "collected": {"shop": "甲"}})
        resumed.append(copy.deepcopy(resume))
        inside_breakpoint.set()
        if hold["on"]:
            # 停在断点里不放手：第二条答案就是要在这一刻进来。那个上限是给写坏的用例
            # 兜底不挂死的，不是用来抢时序的——时序由 inside_breakpoint / release 会合。
            if not release.wait(30):
                raise RuntimeError("会合点没被放行：这条用例写坏了，不是产品坏了")
        return (f"已提交订单（{resume['state']['item']}），答案={resume['answer']}，"
                f"接自第{resume['state']['step']}步")

    def a3r_calc(expression: str = "1+1"):
        return {"1+1": 2, "2*3": 6}.get(expression, 0)

    entries = {
        "a3r_gate": {"function": a3r_gate, "description": "下一杯咖啡（要验证码）",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        "a3r_calc": {"function": a3r_calc, "description": "算一下",
                     "parameters": {"type": "object",
                                    "properties": {"expression": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    ledger_saved = dict(ex._FREQ_LEDGER)
    try:
        yield {"spent": spent, "resumed": resumed, "llm": llm,
               "inside_breakpoint": inside_breakpoint, "release": release, "hold": hold}
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous
        ex._FREQ_LEDGER.clear()
        ex._FREQ_LEDGER.update(ledger_saved)


def _patch_llm_for_race(monkeypatch, gate):
    """三条模型路（规划 / 子任务 / 汇总）都换成桩，每次调用记进闭包那本账。

    子任务那一条只看消息形状（与 `_orchestrator_with` 同一个口径）：对话里已经有
    "工具执行结果"那一行就说明断点接上了，直接给最终答案；否则就要调 `a3r_gate`。
    """
    def plan_llm(model, messages, **kw):
        gate["llm"].append("plan")
        return json.dumps(["选店并下单"], ensure_ascii=False)

    def task_llm(model, messages, **kw):
        gate["llm"].append("task")
        already = any(str(m.get("content", "")).startswith("工具执行结果") for m in messages)
        if already:
            return "拿到了，继续"
        return _call("a3r_gate", {"item": "拿铁"})

    def summary_llm(model, messages, **kw):
        gate["llm"].append("summary")
        return "最终汇总"

    monkeypatch.setattr("app.agents.planner.get_llm_response", plan_llm)
    monkeypatch.setattr("app.agents.task_agent.get_llm_response", task_llm)
    monkeypatch.setattr("app.agents.orchestrator.get_llm_response", summary_llm)


def _parked(client, headers, goal=_GOAL) -> dict:
    """把一条任务跑到"停在工具断点上等人"。单个请求，用 TestClient 无妨。"""
    res = client.post("/v1/agent/orchestrate", json={"goal": goal}, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _answer_sync(client, headers, task_id, answer) -> dict:
    """单条**顺序**的答案调用（会合之后的补问、跨用户那条的阳性对照）。"""
    res = client.post("/v1/agent/orchestrate",
                      json={"goal": _GOAL, "task_id": task_id, "user_answer": answer},
                      headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


async def _await_event(ev, timeout: float = 30.0) -> bool:
    """在事件循环里等一个 threading 事件：会合点必须真的会合，不许拿 sleep 猜时机。

    等法走默认 executor，因为 `Event.wait()` 是阻塞调用——摁在循环上等等于用一条阻塞去
    测并发，而本仓那条纪律正是"阻塞不许上事件循环"。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, ev.wait, timeout)


def _async_client():
    """ASGITransport + `raise_app_exceptions=False`。

    后者是要的：处理器里飘出去的异常在这里必须现形为一个 **500 响应**，而不是把测试
    本身炸掉——这几条用例判的正是"客户端看到了什么"。
    """
    from app.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver", timeout=120.0)


async def _answer(client, headers, task_id, answer):
    return await client.post("/v1/agent/orchestrate",
                             json={"goal": _GOAL, "task_id": task_id,
                                   "user_answer": answer},
                             headers=headers)


def test_two_concurrent_answers_for_one_parked_task_pay_steps_1_to_3_only_once(
        race_tools, isolated_tasks, client, enforced, monkeypatch):
    """⭐ 审查员那一格的原样复现：两条答案同时回答同一个停点，钱只许花一次。

    会合是确定的，不靠 sleep：第一条答案**已经走进那个工具**（`inside_breakpoint` 由道具
    自己点亮）并且不放手，第二条才被放出去——这一刻任务状态必然是 RUNNING，也就是审查员
    复现出来的那个窗口。四条判据：
    1. 两条都 200（不许有 500）；
    2. 世界那本账 `spent` 只有一笔（重复付费就红）；
    3. 断点只被接上**一次**（`resumed` 一条），子任务模型调用也只两次；
    4. 两个响应不许自相矛盾：恰好一条赢（completed），恰好一条拿到"这条任务正在执行、
       你的答案没有被采用"（`accepted is False` + `reason=already_in_flight`），
       而落败那条不是假装成功（没有 final_answer、没有把任务写成 failed）。
    """
    gate = race_tools
    _patch_llm_for_race(monkeypatch, gate)
    ha = enforced("并发甲")

    parked = _parked(client, ha)
    assert parked["status"] == "waiting_user" and parked["can_resume"] is True
    tid = parked["task_id"]
    assert gate["spent"] == ["step1-3"], f"停点之前本该只有这一笔：{gate['spent']}"

    async def scenario():
        async with _async_client() as c:
            first = asyncio.ensure_future(_answer(c, ha, tid, "8888"))
            won = await _await_event(gate["inside_breakpoint"])
            assert won, "前提没成立：第一条答案没走到工具断点，整条用例就是空测"
            second = asyncio.ensure_future(_answer(c, ha, tid, "1234"))
            try:
                r2 = await second
            finally:
                gate["release"].set()        # 无论断言怎么红都不许把线程留死
            r1 = await first
            return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    bodies = [r1.json(), r2.json()]

    assert gate["spent"] == ["step1-3"], (
        f"前三步被重复付费：{gate['spent']}——两条答案里有一条把整条子任务从第 1 步重跑了")
    assert len(gate["resumed"]) == 1, (
        f"同一个断点被接了 {len(gate['resumed'])} 次，只有一枚答案该接到它")
    assert gate["llm"].count("task") == 2, (
        f"子任务模型调用数不对（应为停点 1 + 赢家 1）：{gate['llm']}")

    losers = [b for b in bodies if b.get("accepted") is False]
    assert len(losers) == 1, f"两条响应必须恰好一条认账：{bodies}"
    loser = losers[0]
    winner = bodies[1 - bodies.index(loser)]
    assert loser["reason"] == "already_in_flight", loser
    assert loser["status"] == "running", f"落败者该拿到在飞状态，不是另一个终态：{loser}"
    assert loser.get("final_answer") is None, "落败者不许拿到成功形状"
    assert winner["status"] == "completed", winner
    assert winner["final_answer"] == "最终汇总"

    task = ts.get_task(tid)
    assert task.status is ts.TaskStatus.COMPLETED, task.status
    assert len(task.results) == 1, f"一条子任务却攒了两条结果：{task.results}"
    assert task.error is None, "落败的那一路不许把已完成的任务改写成失败"

    # 认领没有漏还：漏还的症状是"这条任务以后谁也续不上"，所以用一次真调用验它，
    # 不只看内部集合。
    follow = _answer_sync(client, ha, tid, "9999")
    assert follow["status"] == "completed", f"跑完之后再问一次却被当成还在飞：{follow}"
    assert follow.get("accepted") is not False
    assert tid not in orch_mod._EXECUTING, "执行权没还：这条任务以后谁也进不来"
    assert gate["spent"] == ["step1-3"], "补问那一次又花了一笔钱"
    # 答案不落盘（与 A3 既有的那条同一判据）：两条答案里输的那一枚连过手都不该留下值
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))
    assert "8888" not in json.dumps(on_disk, ensure_ascii=False)
    assert "1234" not in json.dumps(on_disk, ensure_ascii=False)


def test_two_answers_that_both_read_waiting_user_leave_exactly_one_owner(
        race_tools, isolated_tasks, client, enforced, monkeypatch):
    """⭐ 更窄的那一格：两条请求都在翻状态**之前**读到了 waiting_user。

    会合点搬进读侧——`_tool_suspension`（WAITING_USER 那一支唯一会调的那次读游标，
    `orchestrator.py:139`）包一层 `threading.Barrier(2)`：两条请求必然都在这一行读到
    同一份 waiting_user，然后一起放行。这正是"两个都看见在等人、两个都动手"的交错，与
    上一条的"一个已翻状态"互补，两条合起来才盖住审查员说的那两个窗口。

    这一族不停在断点里（`hold["on"] = False`）：赢家要跑到底，两条响应才都回得来。
    判据因此写成**交错无关**的四条：断点只被接一次、`spent` 只一笔、结果表只有一条、
    恰好一条认账。
    """
    gate = race_tools
    gate["hold"]["on"] = False
    _patch_llm_for_race(monkeypatch, gate)
    ha = enforced("并发丙")

    parked = _parked(client, ha)
    tid = parked["task_id"]
    assert parked["status"] == "waiting_user"

    real_reader = orch_mod._tool_suspension
    barrier = threading.Barrier(2, timeout=30)
    met: list = []
    met_lock = threading.Lock()

    def spy(task):
        seen = real_reader(task)         # 读到 waiting_user 这一刻，两条都还没动手
        if seen is None:
            return None
        with met_lock:
            met.append(task.task_id)
            join = len(met) <= 2         # 只有那两条并发答案参加会合
        if join:
            # 后面还会第三次走到这一行（落败那一格的响应也读一次游标），所以会合的
            # 名额只给前两条：Barrier 是可复用的，让它再等一轮等于把用例拖慢一个
            # timeout，而多等的那一位根本不是要会合的那两条。
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return seen

    monkeypatch.setattr(orch_mod, "_tool_suspension", spy)

    async def scenario():
        async with _async_client() as c:
            both = await asyncio.gather(_answer(c, ha, tid, "8888"),
                                        _answer(c, ha, tid, "1234"))
            return both, [r.status_code for r in both]

    bodies, codes = asyncio.run(scenario())
    assert len(met) == 2, f"前提没成立：只有一条请求走到读游标那一行（{met}），这是空测"
    assert codes == [200, 200], f"并发答案里飘出了服务端异常：{codes}"

    assert len(gate["resumed"]) == 1, (
        f"同一个断点被接了 {len(gate['resumed'])} 次：两条答案都动了手，"
        "那一笔验证码就等于发了两遍")
    assert gate["spent"] == ["step1-3"], gate["spent"]
    parsed = [b.json() for b in bodies]
    losers = [b for b in parsed if b.get("accepted") is False]
    assert len(losers) == 1, f"两条都该有一个认账：{parsed}"
    loser = losers[0]
    # 这一格不锁死具体是哪一个：赢家跑完后才进来的落败者看到的是 breakpoint_taken
    # （断点已被别人用过），正卡在会合点上的看到的是 already_in_flight。两种都是
    # "你的答案没生效"，都不重跑子任务。
    assert loser["reason"] in ("already_in_flight", "breakpoint_taken"), loser
    assert ts.get_task(tid).status is ts.TaskStatus.COMPLETED
    assert len(ts.get_task(tid).results) == 1, "同一条子任务被记了两次结果"


def test_an_answer_whose_breakpoint_was_just_taken_does_not_rerun_the_subtask(
        race_tools, isolated_tasks, client, enforced, monkeypatch):
    """⭐ 认领锁挡住的第二个窗口：读完 waiting_user 之后、进锁之前，断点被人接走了。

    `test_two_concurrent_answers_*` 那条靠 `_EXECUTING` 这张表挡（另一条正占着执行权），
    而这一条要挡的是**表帮不上忙**的那一瞬：赢家已经跑完并把位还了，输家这时候才进锁。
    只加"有没有人占着"那一判据的话，这一路会顺着 `run` 里那句通用的"恢复任务 … 进度:
    0/1"把整条子任务从第 1 步重跑——审查员复现出来的正是这一笔钱。

    窗口是**造出来的**，不靠线程抢：把 `_tool_suspension` 包一层，就在这条请求读完
    游标的那一刻把状态翻成 running（等价于"另一趟在同一瞬间接走了断点"），单次请求
    就能确定地走到那一格。判据：200、`accepted is False`、`reason ==
    "breakpoint_taken"`、`spent` 与 `resumed` 一本都不许多——然后同一条任务改回
    waiting_user 再答一次，**确实**接得上（证明这一格不是"永远拒绝"）。
    """
    gate = race_tools
    gate["hold"]["on"] = False
    _patch_llm_for_race(monkeypatch, gate)
    ha = enforced("断点甲")

    parked = _parked(client, ha)
    tid = parked["task_id"]
    assert gate["spent"] == ["step1-3"] and gate["resumed"] == []

    real_reader = orch_mod._tool_suspension
    stage = {"taken": False}

    def spy(task):
        seen = real_reader(task)
        if seen is not None and not stage["taken"]:
            stage["taken"] = True
            # 就在"我刚看见它在等人"与"我要去认领它"之间，另一趟把这一跳走了。
            task.set_status(ts.TaskStatus.RUNNING)
        return seen

    monkeypatch.setattr(orch_mod, "_tool_suspension", spy)
    body = _answer_sync(client, ha, tid, "8888")
    # 钱那一行排在最前面是有意的：这一格坏掉的形状就是"前三步又烧一遍"，改动前与
    # 只摘掉那一判据之后都该红在这里，而不是红在一个新字段的名字上。
    assert gate["spent"] == ["step1-3"], (
        f"落败的那条答案把前三步又跑了一遍：{gate['spent']}")
    assert gate["resumed"] == [], "断点已被接走却还把工具又接了一遍"
    assert body.get("accepted") is False, body
    assert body["reason"] == "breakpoint_taken", body
    assert body["status"] == "running", body
    assert gate["llm"].count("task") == 1, f"落败的那一趟还叫了模型：{gate['llm']}"
    assert ts.get_task(tid).results == [], "落败的那一趟还往结果里添了东西"

    # 反例的另一侧：这一格不是"永远拒绝"。停点重新属于这个人 → 同一个答案接得回去。
    task = ts.get_task(tid)
    task.wait_for_user(question="请输入短信验证码", cursor=dict(task.resume_cursor))
    mine = _answer_sync(client, ha, tid, "8888")
    assert mine["status"] == "completed", mine
    assert len(gate["resumed"]) == 1, gate["resumed"]

    # 第三格：执行权被别人占着、而任务此刻正停在**新的**问题上——响应要把那句问题
    # 带回去，否则客户端拿着旧答案一遍遍砸。这一格用真线程抢不稳，直接把状态
    # 造成它与并发时逐字节相同的样子（表里有人 + waiting_user + 游标是新断点）。
    other = ts.get_task(tid)          # 上一跳已经把它写成 completed，另起一条来演
    fresh = ts.Task(goal=_GOAL, subtasks=["选店并下单"], user_id=other.user_id)
    ts.task_store[fresh.task_id] = fresh
    fresh.set_status(ts.TaskStatus.RUNNING)
    fresh.wait_for_user(question="请输入收货地址", cursor={
        "kind": TOOL_SUSPENSION_KIND, "tool_name": "a3r_calc",
        "arguments": {"expression": "1+1"}, "resume_state": {"step": 5},
        "subtask_index": 0})
    spent_now = list(gate["spent"])
    try:
        orch_mod._EXECUTING.add(fresh.task_id)      # 另一趟正占着执行权
        again = _answer_sync(client, ha, fresh.task_id, "8888")
    finally:
        orch_mod._EXECUTING.discard(fresh.task_id)
    assert again["accepted"] is False and again["reason"] == "already_in_flight", again
    assert again["status"] == "waiting_user" and again["can_resume"] is True, again
    assert again["question"] == "请输入收货地址", again
    assert gate["spent"] == spent_now, "被挡下的那一趟不该再花一笔"


def test_a_cancel_racing_an_in_flight_answer_is_refused_not_silently_dropped(
        race_tools, isolated_tasks, client, enforced, monkeypatch):
    """⭐⭐ 工单 #34：答案正在被接回去时，`/v1/tasks/{id}/cancel` 必须 409。

    改动前的形状正是审查员量到的那一格：答案已经走进断点（任务 RUNNING、执行权在
    `_EXECUTING` 里），另一个标签页/管理员打取消，端点只看终态名单 → 200，把状态改成
    CANCELLED。编排循环下一步读到 `task.cancelled` 就收工，用户刚递进去的验证码随任务
    一起被丢掉，且按既有口径不退钱。

    修好之后判据反过来：取消拿到 409、任务一格不改；放行后那枚答案照常跑完
    （completed、断点只接一次、结果只有一条）。这条在旧实现上红在 409 那句——旧实现
    回 200，响应体是 `status=cancelled`。
    """
    gate = race_tools
    _patch_llm_for_race(monkeypatch, gate)
    ha = enforced("取消甲")

    parked = _parked(client, ha)
    tid = parked["task_id"]

    async def scenario():
        async with _async_client() as c:
            first = asyncio.ensure_future(_answer(c, ha, tid, "8888"))
            won = await _await_event(gate["inside_breakpoint"])
            assert won, "前提没成立：答案没走到断点，这条就是空测"
            cancelled = await c.post(f"/v1/tasks/{tid}/cancel", headers=ha)
            gate["release"].set()
            return cancelled, await first

    try:
        cancelled, res = asyncio.run(scenario())
    finally:
        gate["release"].set()

    assert cancelled.status_code == 409, (
        f"答案在飞时取消竟然落下来了：{cancelled.status_code} {cancelled.text[:300]}")
    detail = cancelled.json()["detail"]
    assert "执行中" in detail and "取消未被接受" in detail, detail

    assert res.status_code == 200, (
        f"答案那一路被取消带崩了：{res.status_code} {res.text[:300]}")
    body = res.json()
    assert body["status"] == "completed", body
    assert body.get("accepted") is not False, body
    task = ts.get_task(tid)
    assert task.status is ts.TaskStatus.COMPLETED, task.status
    assert task.cancelled is False, "被拒的取消不许在任务上留下 cancelled 标志"
    assert len(gate["resumed"]) == 1, f"断点没接或接了两次：{gate['resumed']}"
    assert len(task.results) == 1, f"一条子任务却攒了 {len(task.results)} 条结果"
    assert task.error is None, task.error
    # 执行权在 finally 里还干净了：下一次取消该走正常路（这里任务已终态 → warning）。
    again = client.post(f"/v1/tasks/{tid}/cancel", headers=ha)
    assert again.status_code == 200 and again.json()["status"] == "warning", again.text
    assert tid not in orch_mod._EXECUTING, "执行权没还：这条任务以后谁也进不来"


def test_the_failure_handler_cannot_raise_even_when_the_table_refuses_failed(
        race_tools, isolated_tasks, client, enforced, monkeypatch, capsys):
    """⭐ 把 `orchestrator.py:317` 那一跳的"拒"**确定地**造出来，不靠并发撞窗口。

    做法与本文件那条既有变异锁同形：运行期 `monkeypatch.setitem` 把
    `ALLOWED_TRANSITIONS[RUNNING]` 里 FAILED 那一格抹掉（`task_store.py` 一个字节不动），
    再把执行器换成必定抛的道具 → 失败处理器那句 `set_status(FAILED)` 必被拒。
    改动前：处理器里再抛一次 → 端点 500。修好：200 + 响应与日志都说明白 + 任务留在
    running 且**没有**被假装写成 failed。
    """
    gate = race_tools
    gate["hold"]["on"] = False
    _patch_llm_for_race(monkeypatch, gate)
    neutered = frozenset({ts.TaskStatus.RUNNING, ts.TaskStatus.WAITING_USER,
                          ts.TaskStatus.COMPLETED, ts.TaskStatus.CANCELLED})
    monkeypatch.setitem(ts.ALLOWED_TRANSITIONS, ts.TaskStatus.RUNNING, neutered)
    assert ts.can_transition("running", "failed") is False, "变异没生效，这条成了空锁"

    from app import main as main_module

    def boom(subtask, user_id=None, provider_id=None, **kw):
        raise RuntimeError("上游炸了")

    monkeypatch.setattr(main_module.orchestrator.executor, "execute_task", boom)
    ha = enforced("失败甲")

    async def scenario():
        async with _async_client() as c:
            return await c.post("/v1/agent/orchestrate", json={"goal": _GOAL}, headers=ha)

    res = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert res.status_code == 200, (
        f"失败处理器里那一跳又飘出端点了：{res.status_code} {res.text[:300]}")
    body = res.json()
    assert body["status"] == "running", f"跳转被拒却报出别的状态：{body}"
    assert "被状态机拒" in body["message"], body
    assert "IllegalTaskTransition" in out, f"日志里没留这一句：{out!r}"
    task = ts.get_task(body["task_id"])
    assert task.status is ts.TaskStatus.RUNNING
    assert task.error is None, "被拒的那一跳不许留下半个 failed 形状"
    assert ts.get_task(body["task_id"]).to_dict()["status"] == "running"


def test_a_stranger_answering_a_parked_task_gets_the_same_answer_as_a_missing_id(
        race_tools, isolated_tasks, client, enforced, monkeypatch):
    """⭐ 审查员 N1：本文件 28 条用例全用同一个 user id，这里补一条跨用户的。

    用户 B 拿 A 的真 `task_id` + 猜的答案来答 A 的停点，判据三条：
    1. 跑不了 A 的断点（A 的任务逐字节没动，`resumed` 一条都不许多）；
    2. 读不到 A 游标里的那句话（题面刻意写成只有 A 见过的"请输入甲的一次性密码"）；
    3. 与一个根本不存在的 id 得到**同一个答案**——掩掉 task_id 之后两份响应体逐字节
       相同、状态相同；一旦给非属主换一句话或换一个状态，任务表就成了 uuid 探测器。
    阳性对照同场跑：同一枚 id、同一个答案换成 A 自己发，**确实**从断点接上了——否则
    上面三条在"谁都接不上"的坏实现下也全绿，那叫 vacuous。
    """
    gate = race_tools
    gate["hold"]["on"] = False
    _patch_llm_for_race(monkeypatch, gate)
    ha = enforced("停点甲")
    hb = enforced("停点乙")
    uid_a = client.get("/v1/auth/me", headers=ha).json()["user_id"]
    uid_b = client.get("/v1/auth/me", headers=hb).json()["user_id"]
    assert uid_a != uid_b, "两个用户撞了，跨用户判据成了空话"

    # A 的任务手工停在断点上：游标就是编排器自己写的那种形状（kind + 工具 + 参数快照 +
    # 断点），题面用只有 A 见过的字符串，读侧认的正是 resume_cursor["question"]。
    task = ts.Task(goal=_GOAL, subtasks=["选店并下单"], user_id=uid_a)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user(question="请输入甲的一次性密码", cursor={
        "kind": TOOL_SUSPENSION_KIND, "tool_name": "a3r_gate",
        "arguments": {"item": "拿铁"}, "resume_state": {"step": 3, "item": "拿铁"},
        "subtask_index": 0})
    tid = task.task_id
    before = copy.deepcopy(task.to_dict())
    spent_before = list(gate["spent"])
    resumed_before = list(gate["resumed"])

    probes = {}
    for label, probe in (("陌生真 id", tid), ("不存在的 id", str(uuid.uuid4()))):
        res = client.post("/v1/agent/orchestrate",
                          json={"goal": "乙自己的目的一", "task_id": probe,
                                "user_answer": "0000"}, headers=hb)
        assert res.status_code == 200, res.text
        probes[label] = (res, res.json())

    def _masked(b):
        return json.dumps({**b, "task_id": "MASKED"}, ensure_ascii=False, sort_keys=True)

    body_real, body_uuid = probes["陌生真 id"][1], probes["不存在的 id"][1]
    assert body_real["status"] == body_uuid["status"], (
        f"存在与否改变了答案 = uuid 探测器：{body_real['status']} vs {body_uuid['status']}")
    assert set(body_real) == set(body_uuid), (
        f"两份响应的键集不一样：{set(body_real) ^ set(body_uuid)}")
    assert _masked(body_real) == _masked(body_uuid), (
        f"非属主与不存在给的不是同一句话：{_masked(body_real)} vs {_masked(body_uuid)}")
    for res, body in probes.values():
        assert body["task_id"] != tid, "B 的请求竟以 A 的任务 id 往下跑了"
        assert "请输入甲的一次性密码" not in res.text, "A 游标里的那句话漏给了 B"
        assert tid not in res.text, "A 的任务 id 出现在 B 的响应体里"
        assert body.get("accepted") is not False, "B 不该拿到那条并发落败的形状"

    assert ts.get_task(tid).to_dict() == before, "B 的两次请求改动了 A 的任务"
    assert ts.get_task(tid).can_resume is True, "A 的停点被陌生人吃掉了"
    assert gate["resumed"] == resumed_before, "B 的答案接了 A 的断点"
    assert gate["spent"] == spent_before + ["step1-3", "step1-3"], (
        f"B 那两趟该各自新建一条自己的任务并各花一笔，A 的那一步不许被跑：{gate['spent']}")

    # 阳性对照：同一枚 id、同一个答案，A 自己发就真的接得上（判上面三条不是空话）
    mine = _answer_sync(client, ha, tid, "8888")
    assert mine["status"] == "completed", mine
    assert mine.get("accepted") is not False
    assert len(gate["resumed"]) == 1, gate["resumed"]
    assert gate["resumed"][0]["answer"] == "8888"
    assert gate["resumed"][0]["state"] == {"step": 3, "item": "拿铁"}
    assert ts.get_task(tid).status is ts.TaskStatus.COMPLETED


def _paths_outside(pairs, allowed_roots):
    """这些实际落点里有几条**不该要它**：不在允许的临时根内，或干脆落在仓库里。

    两条判据叠在一起是有意的：`… 不在临时根` 是本仓那条既有约定的写法
    （`tests/test_test_isolation.py:58` 用的是"不许落在 REPO_ROOT 之下"，因为那才是
    本机生产数据的家；`tests/test_paths.py:134` 用的是"必须在某个根之下"），这里一次
    判两样。第二句尤其要紧：本机的生产 `data/` 就躺在仓库目录里，路径跟着项目根走
    才是真正的那一刀。
    """
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


def test_the_race_harness_writes_only_inside_its_own_temp_root(
        race_tools, isolated_tasks, client, enforced, monkeypatch, tmp_path):
    """⚠️ 本机作业纪律：这个仓库目录**同时是生产机**，本文件新加这一族写的每一字节
    都必须在自己的临时根里。

    判据抄本仓既有那一条：`paths.resolve_all_data_paths()` 是"可变数据到底落在哪"的唯一
    权威清单（`app/core/paths.py:119`，清单判据在 `tests/test_paths.py:213`）。这里在真的
    停点 + 真的答案跑完之后逐条判它是否还在临时根内；再把仓库那份真 `data/tasks.json`
    的字节前后对一遍（路径判据说"不在这里"，这一句直接证明"这里没被写过"）。
    判据自己配反例：同一把尺子量一份"指回仓库真库"的清单必须报红，量一份合法指法必须
    报空——两头都跑，否则上面那段绿是没牙的（本仓为恒绿的假锁付过账）。反例喂的是手工
    造的清单，不是真的把 `TASKS_DB_PATH` 改成仓库路径：那一句本尊试过，它把生产那份
    `data/tasks.json` 的 mtime 动了（内容一模一样，只看字节的判据看不见）。
    """
    from app.core.paths import resolve_all_data_paths

    gate = race_tools
    gate["hold"]["on"] = False
    _patch_llm_for_race(monkeypatch, gate)
    ha = enforced("落点甲")

    before = (REPO_DATA_TASKS.read_bytes() if REPO_DATA_TASKS.exists() else None)
    before_mtime = (REPO_DATA_TASKS.stat().st_mtime_ns
                    if REPO_DATA_TASKS.exists() else None)

    parked = _parked(client, ha)
    _answer_sync(client, ha, parked["task_id"], "8888")

    allowed = [str(tmp_path), tempfile.gettempdir()]
    offenders = _paths_outside(resolve_all_data_paths(), allowed)
    assert not offenders, (
        f"这些运行时数据落到了临时根之外（本机仓库目录就是生产机）：{offenders}")

    after = REPO_DATA_TASKS.read_bytes() if REPO_DATA_TASKS.exists() else None
    after_mtime = (REPO_DATA_TASKS.stat().st_mtime_ns
                   if REPO_DATA_TASKS.exists() else None)
    assert after == before, "真库被测试改了字节——上面那条路径判据没兜住东西"
    # mtime 也判，是因为"把同一份内容原样重写一遍"在字节对比下是隐形的：本仓第一条
    # 写这条用例时反例就是这么写的（把 TASKS_DB_PATH 真的指回仓库），它把
    # data/tasks.json 的 mtime 动了、内容却一模一样，只看字节的判据当场看不见。
    assert after_mtime == before_mtime, (
        f"真库被人写过一次（mtime 从 {before_mtime} 变成 {after_mtime}），"
        "内容与改动前相同不代表没写盘")

    # 反例：同一把尺子必须能量出"指回仓库真库"这一种坏配置——否则上面那段绿是没牙的
    # （本仓为恒绿的假锁付过账）。反例喂给判据的是一份**手工造的**清单，不把
    # TASKS_DB_PATH 真的改成仓库路径：本目录同时是生产机，把真库路径放进环境里哪怕
    # 一瞬，后面任何一次 flush（包括别的夹具的 teardown 里那一句删任务）都会照着那一刻
    # 的环境写盘，那就是把 data/tasks.json 放进射程内。判据要验的是尺子，不是写盘。
    hostile = ("任务清单", os.path.realpath(str(REPO_DATA_TASKS)))
    bad = _paths_outside([hostile], allowed)
    assert hostile in bad, f"反例都没报出来，上面那条绿不算证据：{bad}"
    # 同一把尺子对一份合法的指法必须报空（否则上面那句"报红"只是因为谁都被报红）
    assert _paths_outside([("任务清单", os.environ["TASKS_DB_PATH"])], allowed) == [], (
        f"合法那条也被报了：{os.environ['TASKS_DB_PATH']}")
