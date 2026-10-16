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
"""
import copy
import inspect
import json
import time

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

    这是 `react_agent.py:212` / `task_agent.py` 老写法 `startswith("✓")` 那一族的
    底线判据：三种前缀互不重叠，且挂起那一支**既不是 ✓ 也不是 ✗**。老读法把它
    读成"这次没拿到可用结果"是安全的方向；读成"成功了"才是事故。
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
    （`streaming.py:553`、`pipeline.py`、`react_agent.py:219`），异常一旦外飘立刻被
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

    两个反面都要防：
    - 不 `record_outcome` → 本轮轮次少记一笔 → 成本上限被绕过（本仓那条
      "尝试即计数"的口径，见 tools/executor.py:16-18）。
    - 先 `clip_output` 再抛 → 那句根本没给模型看的问话还吃掉了本轮输出预算。
    负向锁：删掉 `task_agent.py` 里 `guard.record_outcome(tool_name, ok=bool(outcome.ok))`
    那一行 → `tool_rounds == 1` 立刻红。
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
    assert [e["type"] for e in guard.guard_events] == [], guard.guard_events


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
