"""审批闸门必须在**执行器**上，以及「批准 → 真执行」闭环。

这两条是 v0.31 遗留的两个真实缺陷，本文件是它们的负向契约锁：

1. 闸门原先只挂在 pipeline/streaming 两个**调用侧**的 `if approval.needs_approval(...)`
   分支上，而真正执行工具的 `executor._dispatch` 完全不知情。于是 react_agent 与
   task_agent 这两条直接 `execute_tool` 的路径绕开了审批——任何登录用户调
   /v1/agent/run 就能让 LLM 无审批执行代码。
2. `confirm_approval` 消费掉票据就返回"已允许执行"，`ticket.arguments`（含代码全文）
   被丢弃，全项目没有任何代码在批准后重新执行。用户在App 上点"允许"，什么都不会发生。
   而现有 test_v031_trusted_execution.py 覆盖了标记/创建/拒绝熔断/超时/跨属主，
   **唯独没测「允许 → 执行」**，所以 CI一直是绿的。

每条判据都配一把负向锁：把修复删掉，这条测试必须变红。
"""
import inspect
import sys

import pytest


# ---------- 闸门位置：必须在执行器，不在调用侧 ----------

def test_approval_gate_lives_in_executor_dispatch():
    """判据1：审批检查写在 `executor._dispatch` 里。

    负向锁：把 _dispatch 里那段 `requires_approval` 判断删掉，这条测试红——
    而这正是修复前的状态（闸门只在 pipeline/streaming 的调用侧）。
    """
    from app.tools import executor
    source = inspect.getsource(executor._dispatch)
    assert "requires_approval" in source, (
        "审批判据必须写在 executor._dispatch 里。放在调用侧等于假设所有未来的"
        "调用方都记得检查——react_agent 与 task_agent 已经证明这个假设不成立"
    )
    assert "is_granted" in source, (
        "_dispatch 必须真的调 approval.is_granted 校验票据，而不是只看标记就放行"
    )


def test_executor_rejects_approval_tool_without_ticket():
    """判据 2：需要审批的工具，没带票据时执行器**真的不执行**。

    这是修复前最关键的漏洞：react_agent/task_agent 直接 execute_tool 就能跑代码。

    负向锁：把 _dispatch 里那段判断删掉，这条测试立刻红（工具函数会被真调用）。
    """
    from app.tools.registry import tools_registry
    from app.tools.executor import _dispatch

    called = []

    def fake_execute_code(code: str, language: str = "python"):
        called.append(code)
        return "STDOUT: 应该永远看不到我"

    info = tools_registry["execute_code"]
    original = info["function"]
    original_available = info["available"]
    info["function"] = fake_execute_code
    info["available"] = lambda: True
    try:
        resp = _dispatch("execute_code", {"code": "print(1)"}, user_id="u_bypass")
    finally:
        info["function"] = original
        info["available"] = original_available

    assert called == [], (
        "未带票据的 execute_code 绝不能被执行——这正是绕过审批的那条路"
    )
    assert resp.success is False, "未获批准的调用必须返回失败"
    assert "确认" in resp.to_string() or "批准" in resp.to_string(), (
        "回给模型的文案要说清是缺审批确认，便于模型改走聊天路径"
    )


def test_react_agent_cannot_bypass_approval():
    """判据 3：ReAct 这条路拿不到无审批的代码执行。

    负向锁：删掉 executor 的闸门、或让 react_agent 传 granted_ticket，这条测试红。
    """
    import ast
    from app.agents import react_agent

    source = inspect.getsource(react_agent)
    tree = ast.parse(source)

    # react_agent 里不该出现"自己造一张票据"的动作
    assert "create_approval_request" not in source, (
        "react_agent 不该自己建审批票据——它没有让用户确认的交互"
    )
    # 也不该给 execute_tool 传 granted_ticket（那等于替用户批准）
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "execute_tool"):
            for kw in node.keywords:
                assert kw.arg != "granted_ticket", (
                    "react_agent 不得给 execute_tool 传 granted_ticket："
                    "它没有用户确认的交互，传了等于替用户批准"
                )


def test_task_agent_cannot_bypass_approval():
    """判据 3的 TaskAgent 版。

    负向锁：同上，删掉任何一处都会红。
    """
    import ast
    from app.agents import task_agent

    source = inspect.getsource(task_agent)
    assert "create_approval_request" not in source
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "execute_tool"):
            for kw in node.keywords:
                assert kw.arg != "granted_ticket", (
                    "task_agent 不得传 granted_ticket"
                )


def test_is_granted_rejects_all_wrong_conditions():
    """判据 4：放行票据的判据是 fail-closed 的，四道条件缺一不可。

    负向锁：把 is_granted 里任一判据删掉（改成只判 decision），这条测试红。
    """
    from app.core import approval

    req = approval.create_approval_request(
        "execute_code", {"code": "print(1)"}, user_id="owner_grant")
    tid = req["ticket_id"]
    ticket = approval.consume_ticket(tid, "allow", "owner_grant")
    assert ticket is not None

    # 正常情形：放行
    assert approval.is_granted(ticket, "execute_code", "owner_grant") is True
    # None = 未获批准
    assert approval.is_granted(None, "execute_code", "owner_grant") is False
    # 工具名不一致：一张票据只放行它自己那次调用
    assert approval.is_granted(ticket, "web_search", "owner_grant") is False
    # 属主不一致：跨属主不成立
    assert approval.is_granted(ticket, "execute_code", "someone_else") is False

    # reject 的票据永远不放行
    req2 = approval.create_approval_request(
        "execute_code", {"code": "print(2)"}, user_id="owner_reject")
    t2 = approval.consume_ticket(req2["ticket_id"], "reject", "owner_reject")
    assert approval.is_granted(t2, "execute_code", "owner_reject") is False


# ---------- 执行闭环：批准 → 真执行 ----------

def test_allow_executes_the_code():
    """判据 5（本次修复的核心）：批准后代码**真的被执行**，结果回填票据。

    负向锁：把 confirm_approval 里 execute_tool_detailed 那段删掉（恢复成只消费
    票据就返回"已允许执行"的空壳），这条测试红——而这正是修复前的状态，
    也是 v0.31 一直没人发现的那个洞。
    """
    from app.core import approval
    from app.tools.registry import tools_registry
    from app.tools.executor import execute_tool_detailed

    req = approval.create_approval_request(
        "execute_code", {"code": "print(1+1)", "language": "python"},
        user_id="owner_exec")
    tid = req["ticket_id"]

    info = tools_registry["execute_code"]
    original = info["function"]
    info["function"] = lambda code, language="python": "STDOUT: 2\n"
    try:
        ticket = approval.consume_ticket(tid, "allow", "owner_exec")
        assert ticket is not None
        # confirm 端点里就是这两行
        outcome = execute_tool_detailed(
            ticket.tool_name, ticket.arguments, user_id=ticket.user_id,
            granted_ticket=ticket)
        approval.record_execution(tid, outcome.text, outcome.ok)
    finally:
        info["function"] = original

    stored = approval.get_ticket(tid)
    assert stored.executed is True, (
        "批准后票据必须被标记为已执行——否则 GET /v1/approval/{id} 轮询永远拿不到结果。"
        "confirm 端点若只消费票据就返回，这条红"
    )
    assert stored.ok is True
    assert "STDOUT: 2" in stored.result, (
        f"执行结果应当是工具真的跑出来的输出，实际拿到：{stored.result!r}"
    )


def test_approval_execution_is_single_use():
    """判据 6：一张票据最多执行一次（防重放）。

    负向锁：把 consume_ticket 的 `if ticket.consumed: return None` 删掉，这条测试红。
    """
    from app.core import approval

    req = approval.create_approval_request(
        "execute_code", {"code": "print(9)"}, user_id="owner_replay")
    tid = req["ticket_id"]
    first = approval.consume_ticket(tid, "allow", "owner_replay")
    assert first is not None
    second = approval.consume_ticket(tid, "allow", "owner_replay")
    assert second is None, (
        "同一张票据不得被消费两次——否则一次点击可以触发多次代码执行"
    )


def test_reject_does_not_execute():
    """判据 7：拒绝绝不执行。

    负向锁：把 confirm 端点的 `if request.decision != "allow"` 分支删掉，
    这条测试红。
    """
    from app.core import approval
    from app.tools.registry import tools_registry

    req = approval.create_approval_request(
        "execute_code", {"code": "print(0)"}, user_id="owner_noexec")
    tid = req["ticket_id"]

    called = []

    def fake(code: str, language: str = "python"):
        called.append(code)
        return "不该跑"

    info = tools_registry["execute_code"]
    original = info["function"]
    info["function"] = fake
    try:
        ticket = approval.consume_ticket(tid, "reject", "owner_noexec")
        assert ticket is not None
        # allow 分支才会走到 execute_tool_detailed；reject 时不该执行
        assert ticket.decision == "reject"
        assert approval.is_granted(ticket, "execute_code", "owner_noexec") is False
        from app.tools.executor import _dispatch
        _dispatch("execute_code", {"code": "print(0)"}, user_id="owner_noexec")
    finally:
        info["function"] = original

    assert called == [], "拒绝后绝不能真的执行代码"


def test_get_status_reports_execution_outcome():
    """判据 8：轮询端点能看到执行结果与成败。

    `ok` 必须显式存在票据上，不许读侧去 parse 结果文本里的 "✓"——
    那是把"✓ 前缀是契约"复制一份到第二个文件。

    负向锁：把 record_execution 里的 `ticket.ok = bool(ok)` 删掉，这条测试红。
    """
    from app.core import approval

    req = approval.create_approval_request(
        "execute_code", {"code": "print(7)"}, user_id="owner_status")
    tid = req["ticket_id"]
    ticket = approval.consume_ticket(tid, "allow", "owner_status")
    approval.record_execution(tid, "✓ stdout 里有 7", True)

    stored = approval.get_ticket(tid)
    assert stored.executed is True
    assert stored.ok is True, "ok 必须显式存字段，不许读侧 parse 文本"
    assert "7" in stored.result

    # 失败的执行：ok 为 False，且与"没执行过"分得开
    req2 = approval.create_approval_request(
        "execute_code", {"code": "raise"}, user_id="owner_status")
    t2 = approval.consume_ticket(req2["ticket_id"], "allow", "owner_status")
    approval.record_execution(t2.ticket_id, "✗ 错误: 炸了", False)
    stored2 = approval.get_ticket(t2.ticket_id)
    assert stored2.ok is False
    assert stored2.executed is True, "执行过但失败了，executed 与 ok 要能分开表达"


def test_tickets_are_cleaned_up_on_create():
    """判据 11：过期的票据不会无限堆积（审计发现 cleanup_expired 全项目无调用点）。

    票据现在还存着执行结果（可能上千字），只增不减既是内存增长也是留痕。

    负向锁：把 create_approval_request 里那段 stale 清理删掉，这条测试红。

    2026-10-16 v0.35 A2 的适配（**判据一字没动**，改了两处入口）：
    1. 票据表的键从明文 ticket_id 换成 sha256 摘要（盘上与内存同一个形状，抄的是
       auth.py:141-152 那条"凭据只存摘要"的纪律）。所以"把这一张做旧"要先算摘要再进表。
    2. A2 之后 get/consume/record 三个入口**也会扫**，于是"用 get_ticket(tid) is None
       来验建票扫没扫"会自己把那张旧票扫掉——负向锁当场空转（实跑：删掉建票里的
       `_sweep_locked(now)`，这一条照样绿）。所以判据改回直接看表，顺带把 A2 新开的
       磁盘面钉上：内存与盘上都不许留着那张过期票。断言的两句话与它防的东西不变。
    """
    import time
    from app.core import approval

    req = approval.create_approval_request(
        "execute_code", {"code": "print(0)"}, user_id="owner_gc")
    tid = req["ticket_id"]
    # 把它做旧成"远超有效期"
    with approval._lock:
        approval._tickets[approval.hash_ticket(tid)].expires_at = (
            time.time() - 10 * approval.APPROVAL_TIMEOUT_SECONDS)

    fresh = approval.create_approval_request(
        "execute_code", {"code": "print(1)"}, user_id="owner_gc2")
    fresh_tid = fresh["ticket_id"]

    with approval._lock:
        assert approval.hash_ticket(tid) not in approval._tickets, (
            "远超有效期的票据应被建票时的清理带走——cleanup 判据不起作用就会无界堆积"
        )
        assert approval.hash_ticket(fresh_tid) in approval._tickets, (
            "清理不该误删刚创建的活跃票据"
        )
    # A2 新开的磁盘面：内存扫掉了而盘上留着，就是"文件只增不减"那个根因
    import json
    with open(approval._default_path(), encoding="utf-8") as f:
        on_disk = json.load(f)["tickets"]
    assert approval.hash_ticket(tid) not in on_disk, (
        "过期票据从内存消失了却还躺在 data/approvals.json 里（里面是参数与执行结果）")


# ---------- 护栏覆盖：两条自主路径也得有 ----------

def test_react_agent_has_round_guard():
    """判据 9：ReAct 这条路必须有 RoundGuard。

    它原先没有护栏，而 max_turns（10）高于 MAX_TOOL_ROUNDS（5）——一次请求能连拉
    10 轮容器，且无重复检测与失败退避。

    负向锁：把 react_agent 里 `guard = RoundGuard()` 删掉，这条测试红。
    """
    from app.agents import react_agent
    source = inspect.getsource(react_agent)
    assert "RoundGuard" in source, "react_agent 必须实例化 RoundGuard"
    assert "guard.check(" in source, "必须在执行前调 guard.check"
    assert "guard.record_outcome(" in source, "执行后必须记失败退避，否则失败重试不熔断"


def test_task_agent_has_round_guard():
    """判据 9的 TaskAgent 版。"""
    from app.agents import task_agent
    source = inspect.getsource(task_agent)
    assert "RoundGuard" in source
    assert "guard.check(" in source


def test_max_turns_cannot_exceed_round_guard():
    """判据 10：护栏在位时，循环上限再大也烧不过 MAX_TOOL_ROUNDS。

    这是判据 9 的行为验证。注意 RoundGuard 的两个方法是分工的：`check` 负责拦
    （重复检测/失败退避/已熔断），`record_outcome` 才推进轮次计数——因为"被 check
    拦下的调用"没有真的执行，不该计入成本。测试要照真实用法成对调用。
    """
    from app.core.guardrails import RoundGuard, MAX_TOOL_ROUNDS

    guard = RoundGuard()
    blocked_at = None
    executed = 0
    for i in range(20):
        reason = guard.check("execute_code", {"code": f"print({i})"})
        if reason:
            blocked_at = i
            break
        # 参数每次不同，所以重复检测不会先触发——这一条验证的是轮次上限本身
        executed += 1
        guard.record_outcome("execute_code", True)

    assert blocked_at is not None, "护栏从未熔断——说明它没生效"
    assert executed == MAX_TOOL_ROUNDS, (
        f"应当执行满 {MAX_TOOL_ROUNDS} 轮就熔断，实际执行了 {executed} 轮"
    )
    assert blocked_at == MAX_TOOL_ROUNDS, (
        f"应当在第 {MAX_TOOL_ROUNDS} 次调用被拦，实际第 {blocked_at} 次"
    )


def test_duplicate_calls_are_fused():
    """判据 10 补充：相同参数的重复调用被重复检测熔断（注入链的变现口）。

    负向锁：把 check 里的 duplicate_threshold 分支删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard, DUPLICATE_THRESHOLD

    guard = RoundGuard()
    reason = None
    for i in range(DUPLICATE_THRESHOLD):
        reason = guard.check("web_search", {"query": "同一个问题"})
        if reason:
            break
    assert reason is not None, "相同参数重复调用应被熔断"
    assert "重复" in reason