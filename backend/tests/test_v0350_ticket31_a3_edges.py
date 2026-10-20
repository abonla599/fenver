"""工单 #31：A3 协议的三条边角（N2 / N4 / N5）。

N2 —— `_resume_refusal` 只判 `isinstance(state, dict)`，不判这份断点能不能过一次
JSON 往返。`state={"o": object()}` 在挂起那一侧被如实报成 `can_resume=False`，到了
续跑这一侧却一句拒绝都没有：工具照跑、频控照吃一格、钱照花。本文件用一把**带频控档**
的道具证明"拒绝发生在扣预算之前"，并用可往返的状态做阳性对照。

N4 —— 工单原文是"同轮两次挂起必熔断；没有任何测试断言它"。⚠️ 这句落在今天的代码上
**字面不成立**，而且是刻意的不成立：工单 #29（已合入 develop，四把锁在
`tests/test_v0350_ticket29_guardrail_third_state.py`）把"挂起"定成护栏里的**中性**一格
——不跳退避、不熔断、只另记一本 `suspend` 账，理由是用户两次"请把验证码发我"之后
工具就被熔断，正是 #29 要修的那条事故。照字面实现 N4 等于回退 #29 并让那四把锁同时变红。
于是这里按**能自洽、且有牙**的那一面补上缺的断言：
  (a) 工具连着两次交出**执行器不得不折成失败**的挂起（没给问句 / 自相矛盾）→ 第 2 次
      必须熔断，Threshold 就是 `guardrails.CONSECUTIVE_FAIL_THRESHOLD`。这一面此前
      一条锁都没有：`_settle_suspension` 的"折成失败"与护栏的退避算术之间的接缝。
  (b) 反方向对照：两次**接得回去**的挂起仍然中性（#29 的不变量），顺手钉住调用点的
      写法（`ok=outcome.ok, suspended=outcome.suspended`）没有传反。
两条一起读才是"阈值在挂起附近仍然有牙、而且只在它该咬的时候咬"。

N5 —— 续跑种子（`task_agent.py` 的 `resumed_tool` 一问一答）在护栏实例创建**之前**
被拼进 messages，于是这一份工具结果绕过了 `guard.clip_output`：同一轮里"续跑结果 +
后续工具输出"的累计字数比别的路径多出一整份（16000）。修法是先建 guard 再拼种子，
让轮预算看见它；执行器那一步已经把 per-call 裁到 4000，这里记的是**轮**预算，不是
重复计费。
"""
import json

import pytest

import app.agents.task_agent as ta_mod
from app.core.guardrails import CONSECUTIVE_FAIL_THRESHOLD, RoundGuard
from app.tools import executor as ex
from app.tools.registry import tools_registry
from app.tools.response import ToolSuspended


@pytest.fixture
def t31_tools(monkeypatch):
    """三条道具：可续的、问不出话的（被折成失败）、以及一条带频控档的续跑靶子。

    `t31_gated` 那一条把频控档临时写进 `_FREQ_LIMITS`：只有它的账本看得见"跑了一格"，
    N2 的判据才不是自报的。用独立名字而不是借 `execute_code`/`plan_add`：那两个各有
    审批面/日程副作用，借来当探针会把别的东西一起搅进判据。
    """
    calls = []

    def t31_gate(item: str = "拿铁", resume: dict = None):
        calls.append(("gate", item, resume))
        if resume is None:
            raise ToolSuspended("请输入短信验证码", {"step": 3, "item": item})
        return f"已提交（答案={resume['answer']}）"

    def t31_silent(item: str = "拿铁"):
        calls.append(("silent", item, None))
        raise ToolSuspended("", {"step": 1, "item": item})

    entries = {
        "t31_gated": {"function": t31_gate, "description": "续跑靶子",
                      "parameters": {"type": "object",
                                     "properties": {"item": {"type": "string"}}},
                      "available": None, "needs_user": False,
                      "requires_approval": False, "suspends": True},
        "t31_silent": {"function": t31_silent, "description": "问不出话的挂起",
                       "parameters": {"type": "object",
                                      "properties": {"item": {"type": "string"}}},
                       "available": None, "needs_user": False,
                       "requires_approval": False, "suspends": True},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    monkeypatch.setitem(ex._FREQ_LIMITS, "t31_gated", (600.0, 10))
    ledger_saved = dict(ex._FREQ_LEDGER)
    ex._FREQ_LEDGER.clear()
    try:
        yield calls
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous
        ex._FREQ_LEDGER.clear()
        ex._FREQ_LEDGER.update(ledger_saved)


# ---------- N2：形状对 ≠ 带得过一次重启 ----------

def test_n2_an_unserializable_breakpoint_is_refused_before_the_tool_or_the_budget(
        t31_tools):
    """⭐ `state={"o": object()}`：拒绝、工具一次都不跑、频控一格都不吃。

    删掉 `_resume_refusal` 里那句 `_state_carries(state)`，本条红在 `calls`（工具被
    调了）、`tool_ran`（执行器说答案递进去了）与频控账本（吃掉了用户的一格额度）。
    """
    outcome = ex.execute_tool_detailed(
        "t31_gated", {"item": "拿铁"}, user_id="u-31",
        resume={"state": {"o": object()}, "answer": "8888"})

    assert outcome.suspended is False and outcome.ok is False, outcome
    assert outcome.tool_ran is False, "断点带不过重启，答案绝不该进函数体"
    assert "JSON" in outcome.text, outcome.text
    assert t31_tools == [], f"工具被跑了：{t31_tools}"
    assert ("t31_gated", "u-31") not in ex._FREQ_LEDGER, (
        "被拒的续跑吃掉了这个人的频控额度——判据必须排在 _freq_gate 之前")

    # 同一条判据在挂起那一侧是 `can_resume=False`：两处现在说同一句话。
    fresh = ex.execute_tool_detailed("t31_gated", {"item": "拿铁"}, user_id="u-31")
    assert fresh.suspended is True and fresh.suspension["can_resume"] is True, fresh


def test_n2_control_a_round_trippable_breakpoint_still_runs_and_pays_one_slot(
        t31_tools):
    """阳性对照：可往返的断点照常续跑——否则上面那条只是因为"续跑永远坏着"而绿。"""
    outcome = ex.execute_tool_detailed(
        "t31_gated", {"item": "拿铁"}, user_id="u-31",
        resume={"state": {"step": 3, "item": "拿铁"}, "answer": "8888"})
    assert outcome.ok is True and outcome.tool_ran is True, outcome
    assert "答案=8888" in outcome.text, outcome.text
    assert len(t31_tools) == 1, t31_tools
    assert len(ex._FREQ_LEDGER[("t31_gated", "u-31")]) == 1, (
        "续跑真跑了就该吃一格预算（这条证明频控账本没被 N2 的修法顺手绕开）")


# ---------- N4：阈值在挂起附近仍然有牙 ----------

def _guarded():
    """只隔离 N4 要判的那一格：重复检测与轮次上限抬到够不着。"""
    return RoundGuard(fail_threshold=CONSECUTIVE_FAIL_THRESHOLD,
                      duplicate_threshold=99, max_tool_rounds=99)


def test_n4_two_consecutive_suspensions_the_executor_had_to_settle_as_failures_trip_the_threshold(
        t31_tools):
    """⭐ 工具连着两次说"我要挂起"，但都没给出问句 → 第 2 次必须熔断。

    这一格的"两次挂起"是**工具自己声明的那两次**；执行器按 fail-closed 把它们折成
    失败（`_settle_suspension`），护栏照失败算——这正是 N4 能字面成立的那一面，也是
    此前没有任何断言覆盖的接缝。判据问的是 `guard.check()` 门口给的回话，不是自报。

    删掉护栏的失败退避（或把阈值改成 3），本条红在最后那句 `blocked`。
    """
    guard = _guarded()
    first = ex.execute_tool_detailed("t31_silent", {"item": "拿铁"}, user_id="u-31")
    assert first.suspended is False, "问不出话的挂起必须被折成失败（A3 的 fail-closed）"
    guard.record_outcome("t31_silent", ok=first.ok, suspended=first.suspended)
    assert guard.check("t31_silent", {"probe": 1}) is None, "第一次就熔断，太紧"

    second = ex.execute_tool_detailed("t31_silent", {"item": "美式"}, user_id="u-31")
    guard.record_outcome("t31_silent", ok=second.ok, suspended=second.suspended)
    blocked = guard.check("t31_silent", {"probe": 2})
    assert blocked, f"两次折成失败的挂起没有熔断：阈值是 {CONSECUTIVE_FAIL_THRESHOLD}"
    assert "连续失败 2 次" in blocked, blocked
    assert guard.suspension_counts == {}, (
        "这两次在护栏看来是失败，不是挂起——`suspend` 账不许被写进去")
    assert {"fail_backoff"} <= {e["type"] for e in guard.guard_events}, guard.guard_events


def test_n4_control_two_carryable_suspensions_stay_neutral_as_ticket29_locked(
        t31_tools):
    """反方向：两次**接得回去**的挂起仍然中性（#29 的四把锁，这里从执行器真结果喂）。

    这条同时钉住四个调用点共用的写法：`ok=outcome.ok, suspended=outcome.suspended`。
    挂起时 `outcome.ok` 也是 False——只传 ok 的那份写法会让这里第 2 次就熔断。
    """
    guard = _guarded()
    for item in ("拿铁", "美式"):
        outcome = ex.execute_tool_detailed("t31_gated", {"item": item}, user_id="u-31")
        assert outcome.suspended is True and outcome.ok is False, outcome
        guard.record_outcome("t31_gated", ok=outcome.ok, suspended=outcome.suspended)
    assert guard.check("t31_gated", {"probe": "第三次还问不问"}) is None, (
        "接得回去的挂起把工具熔断了——那是 #29 修掉的那条事故在复活")
    assert guard.suspension_counts == {"t31_gated": 2}, guard.suspension_counts
    assert guard.tool_rounds == 2, "挂起照记轮次（不记就是给重试留免费额度）"


# ---------- N5：续跑种子吃轮输出预算 ----------

def test_n5_the_resume_seed_pays_the_round_output_budget(monkeypatch):
    """⭐ 30000 字的续跑种子必须先过 `clip_output`，模型看到的不能是原文。

    删掉 `guard = RoundGuard()` 前移那一步（或把种子里的 `guard.clip_output` 换回
    裸字符串），本条红在长度与"已截断"那句上：种子会整份 30000 字进 messages，
    而 round 预算（16000）一格都没动。
    """
    seen = []

    def fake_llm(model, messages, **kw):
        seen.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return "done"

    monkeypatch.setattr(ta_mod, "get_llm_response", fake_llm)
    agent = ta_mod.TaskAgent(name="t31", model="m", tools_schema=[], tools={})
    big = "x" * 30000
    assert agent.run("任务", resumed_tool={"tool_name": "web_search",
                                           "result": big}) == "done"

    assert len(seen) == 1, f"模型调用次数不对：{len(seen)}"
    seed = [m for m in seen[0]
            if m["role"] == "user" and str(m["content"]).startswith("工具执行结果: ")]
    assert len(seed) == 1, seen[0]
    body = seed[0]["content"]
    assert len(body) < 20000, (
        f"续跑种子整份进了上下文（{len(body)} 字）：它绕过了 clip_output")
    assert "已截断" in body, f"截断说明没出现，种子可能没走轮预算：{body[-120:]!r}"
    # 一问一答的形状没变（A3 判据 1）：种子前面那一行仍是 assistant 的工具调用标记。
    seed_at = seen[0].index(seed[0])
    assert seed_at >= 1, seen[0]
    prev = seen[0][seed_at - 1]
    assert prev["role"] == "assistant" and "<tool_call>" in prev["content"], prev
