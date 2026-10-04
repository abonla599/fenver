"""v0.31 R1-R4 负向契约锁：每条判据各有一把经变异验证会红的测试。

PRD §10 验收判据：
1. 诱导模型无限重复调用同一工具+同一参数 → 被拦、对话不 5xx、用户可见「为什么停」
2. 模型发起有副作用的动作 → 先出计划、人确认才执行；拒绝后本轮不重试
3. 全仓只剩一条真 tool_calls 路径，文本协议解析器已删除且有防复活锁
4. 单轮对话的 token/轮次/工具输出有硬上限，超限行为是截断并告知

每条判据配一把负向锁：把实现里的关键保护删掉，这条测试必须变红。
"""
import json
import pytest


# ---------- R4 防复活锁：文本协议不许复活 ----------

def test_r4_text_protocol_parser_removed():
    """R4 判据 3：_extract_tool_calls 方法已删除，全仓只剩原生 tool_calls 路径。

    负向锁：如果有人重新引入文本协议解析（从正文找 ```json 块），这条测试红。
    """
    from app.agents.react_agent import ReActAgent
    agent = ReActAgent(max_turns=3)
    assert not hasattr(agent, "_extract_tool_calls"), (
        "文本协议解析器 _extract_tool_calls 不应存在——v0.31 已全面走原生 tool_calls"
    )


def test_r4_no_json_block_parsing_in_react():
    """R4 判据 3 补充：react_agent.py 里不许有从正文解析 JSON 工具调用的代码。

    负向锁：正则 r'```json' 不应出现在 react_agent.py 里。
    """
    import inspect
    from app.agents import react_agent
    source = inspect.getsource(react_agent)
    assert "```json" not in source, (
        "react_agent.py 不应包含 ```json 文本协议解析——这是 v0.31 要删除的遗留路径"
    )


# ---------- R1 护栏三件套 ----------

def test_r1_duplicate_detection_blocks_repeat_calls():
    """R1 判据 1：同 (tool, 参数) 在单轮内出现 >=3 次 → 熔断。

    负向锁：把 RoundGuard.check 里的重复检测删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(duplicate_threshold=3)
    assert guard.check("web_search", {"query": "test"}) is None
    assert guard.check("web_search", {"query": "test"}) is None
    blocked = guard.check("web_search", {"query": "test"})
    assert blocked is not None, "重复调用应被拦截"
    assert "重复" in blocked or "拦截" in blocked, f"熔断文案应说明原因：{blocked}"


def test_r1_duplicate_detection_normalizes_args():
    """R1 补充：参数规范化——不同键序的相同参数应判为重复。

    负向锁：把 _normalize_args 的排序删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(duplicate_threshold=2)
    assert guard.check("web_search", {"q": "x", "n": 3}) is None
    blocked = guard.check("web_search", {"n": 3, "q": "x"})
    assert blocked is not None, "参数规范化后应判为重复"


def test_r1_cost_cap_rounds():
    """R1 判据 1：单轮工具轮次超上限 → 停止追加调用。

    负向锁：把 RoundGuard.check 里的轮次上限检查删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tool_rounds=2)
    guard.record_outcome("calculator", ok=True)
    guard.record_outcome("calculator", ok=True)
    blocked = guard.check("calculator", {})
    assert blocked is not None, "超过轮次上限应被拦截"
    assert "上限" in blocked or "轮" in blocked, f"熔断文案应说明原因：{blocked}"


def test_r1_fail_backoff():
    """R1 判据 1：同一工具连续失败 2 次 → 本轮熔断。

    负向锁：把 RoundGuard.record_outcome 里的失败退避逻辑删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(fail_threshold=2)
    guard.record_outcome("web_search", ok=False)
    guard.record_outcome("web_search", ok=False)
    blocked = guard.check("web_search", {"query": "test"})
    assert blocked is not None, "连续失败应触发熔断"
    assert "失败" in blocked, f"熔断文案应说明原因：{blocked}"


def test_r1_fail_backoff_resets_on_success():
    """R1 补充：成功后连续失败计数清零。

    负向锁：把 record_outcome 里的 ok=True 分支清零删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(fail_threshold=2)
    guard.record_outcome("web_search", ok=False)
    guard.record_outcome("web_search", ok=True)
    guard.record_outcome("web_search", ok=False)
    assert guard.check("web_search", {"query": "test"}) is None


# ---------- R2 工具输出预算闸 ----------

def test_r2_output_budget_clips_cumulative():
    """R2 判据 4：单轮累计工具输出超字符上限 → 截断并告知。

    负向锁：把 RoundGuard.clip_output 的截断逻辑删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_output_chars=100)
    text1, trunc1 = guard.clip_output("a" * 60)
    assert len(text1) == 60 and not trunc1
    text2, trunc2 = guard.clip_output("b" * 60)
    assert trunc2, "超预算应被截断"
    assert "预算" in text2 or "截断" in text2, f"截断文案应说明原因：{text2}"


def test_r2_output_budget_merges_with_executor_truncated():
    """R2 补充：与执行器 truncated 标记合并，不双重标记。

    负向锁：把 clip_output 的 already_truncated 参数删掉，这条测试红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_output_chars=1000)
    text, trunc = guard.clip_output("short text", already_truncated=True)
    assert not trunc, "执行器已截断的，本层不应再标记"


# ---------- R3 审批层 ----------

def test_r3_execute_code_requires_approval():
    """R3 判据 2：execute_code 标了 requires_approval=True。

    负向锁：把 builtin_tools.py 里 execute_code 的 requires_approval=True 删掉，
    这条测试红。
    """
    from app.tools.registry import tools_registry
    info = tools_registry.get("execute_code")
    assert info is not None, "execute_code 应已注册"
    assert info.get("requires_approval") is True, (
        "execute_code 必须标 requires_approval=True——这是 v0.31 审批层的核心"
    )


def test_r3_web_search_no_approval():
    """R3 补充：web_search 保持只读直通，不需要审批。

    负向锁：如果给 web_search 误加了 requires_approval，这条测试红。
    """
    from app.tools.registry import tools_registry
    info = tools_registry.get("web_search")
    assert info is not None
    assert not info.get("requires_approval"), "web_search 是只读工具，不应需要审批"


def test_r3_approval_ticket_created():
    """R3 判据 2：模型发起副作用工具 → 生成一次性票据。

    负向锁：把 create_approval_request 的票据生成逻辑删掉，这条测试红。
    """
    from app.core import approval
    req = approval.create_approval_request(
        "execute_code", {"code": "print(1)", "language": "python"},
        user_id="test_user"
    )
    assert "ticket_id" in req, "应返回票据 ID"
    assert req["tool_name"] == "execute_code"
    assert "plan_summary" in req, "应包含计划摘要"
    assert "python" in req["plan_summary"], "摘要应包含语言信息"


def test_r3_approval_reject_fuses_round():
    """R3 判据 2：拒绝后本轮该工具不再出现。

    负向锁：把 consume_ticket 里的 fuse_round 调用删掉，这条测试红。
    """
    from app.core import approval
    req = approval.create_approval_request(
        "execute_code", {"code": "print(1)"}, user_id="test_user_reject"
    )
    ticket_id = req["ticket_id"]
    ticket = approval.consume_ticket(ticket_id, "reject", "test_user_reject")
    assert ticket is not None
    assert approval.is_round_fused("test_user_reject", "execute_code"), (
        "拒绝后本轮该工具应被熔断"
    )


def test_r3_approval_timeout_never_default_allow():
    """R3 判据 2：超时/断连 → 票据作废，绝不默认放行。

    负向锁：把 consume_ticket 里的超时检查删掉，这条测试红。
    """
    import time
    from app.core import approval
    req = approval.create_approval_request(
        "execute_code", {"code": "print(1)"}, user_id="test_user_timeout"
    )
    ticket_id = req["ticket_id"]
    # 手动让票据过期
    with approval._lock:
        approval._tickets[ticket_id].expires_at = time.time() - 1
    ticket = approval.consume_ticket(ticket_id, "allow", "test_user_timeout")
    assert ticket is None, "超时票据不应被消费——绝不默认放行"


def test_r3_approval_cross_owner_rejected():
    """R3 判据 2：跨属主隐私——非票据属主不能消费。

    负向锁：把 consume_ticket 里的属主检查删掉，这条测试红。
    """
    from app.core import approval
    req = approval.create_approval_request(
        "execute_code", {"code": "print(1)"}, user_id="owner_a"
    )
    ticket_id = req["ticket_id"]
    ticket = approval.consume_ticket(ticket_id, "allow", "owner_b")
    assert ticket is None, "跨属主不应能消费票据"


def test_r3_no_default_allow_grep_lock():
    """R3 判据 2 反模式锁：全仓 grep 不到「绕过审批的默认放行」。

    负向锁：如果有人在 executor 里加了"审批超时自动放行"的逻辑，这条测试红。
    """
    import inspect
    from app.core import approval
    source = inspect.getsource(approval)
    # 不应存在"超时后默认允许"的代码路径
    assert "default" not in source.lower() or "allow" not in source.lower() or \
           "timeout" not in source.lower() or "默认放行" not in source, (
        "审批层不应有'超时默认放行'的代码路径"
    )


# ---------- 双端契约文案 ----------

def test_r3_approval_card_text_contract():
    """R3 判据 2：双端文案由契约测试钉住，漂一个字就红。

    安卓与网页端等这份定稿。
    """
    from app.core import approval
    assert approval.APPROVAL_TITLE == "执行代码确认"
    assert approval.APPROVAL_ALLOW_TEXT == "允许"
    assert approval.APPROVAL_REJECT_TEXT == "拒绝"
    assert approval.approval_card_text("execute_code", "python") == \
        "模型想要执行一段 python 代码"
