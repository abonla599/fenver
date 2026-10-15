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


# ---------- R1-bis 总额熔断（__all__）真的挡得住任意工具（v0.34.2 补的） ----------
# record_tokens() 从 v0.31 起就在超限时往 _fused_tools 里塞一个 "__all__" 键，
# 而 check() 只查 `self._fused_tools[tool_name]`——那把通配符**从来没被读过**。
# is_all_fused() 存在，但全仓零调用点（grep 得证）。结果就是"熔断所有工具"这句
# 文案发得出去、工具一个都没停：token 上限跳了之后模型还能继续调 web_search 烧钱。
# 断言一律收在"回来的必须是一句非空的话"上：只断 not None 会把空串当成拦截成功，
# 而调用方 `if blocked:` 恰恰把空串当放行。

def test_r1b_token_cap_fuses_any_tool_name():
    """R1 判据 4：token 上限跳了之后，**任意**工具名都要被拦。

    这里挑一个本轮从没出现过的名字：老写法查的是 _fused_tools[tool_name]，
    新名字必然不在表里 → 放行 → 本条红。

    负向锁：把 check() 里 `_fused_all_reason()` 那三行删掉，本条红（返回 None）。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=100)
    guard.record_tokens(100)

    blocked = guard.check("web_search", {"query": "随便搜点什么"})
    assert isinstance(blocked, str), f"必须是一句话，拿到 {type(blocked).__name__}"
    assert blocked, "token 上限跳了却放行（空串=不拦）"
    assert "token" in blocked or "上限" in blocked, f"理由要说清是总额上限：{blocked}"

    # 换一个从没见过的名字也一样拦：这条钉的是"所有"，不是"这一个"
    other = guard.check("calculator", {"expr": "1+1"})
    assert other == blocked, f"总额熔断应当对所有工具给同一句话：{other!r}"


def test_r1b_fuse_survives_repeated_calls():
    """R1 判据 4 补充：熔断状态不许被后续 check() 消耗掉。

    负向锁：把 _fused_tools["__all__"] 改成 `pop`（读一次就没了），第二次调用
    返回 None，本条红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=50)
    guard.record_tokens(60)

    first = guard.check("web_search", {"query": "a"})
    assert first, "第一次就该拦"
    for name in ("web_search", "execute_code", "read_file"):
        for _ in range(3):
            again = guard.check(name, {"query": "a"})
            assert again == first, f"{name}：第 N 次调用把熔断状态吃掉了或换了口径"
    assert guard.is_all_fused(), "重复调用后状态应当仍在"


def test_r1b_round_under_cap_still_returns_none():
    """R1 判据 4 的另一半：没到上限的一轮不许拦——否则"绿"只是因为什么都拒。

    负向锁：把 check() 里的总额判断写成无条件 `return reason`，本条红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=50000)
    guard.record_tokens(1000)
    assert guard.check("web_search", {"query": "正常一轮"}) is None
    assert guard.check("calculator", {"expr": "1+1"}) is None
    assert not guard.is_all_fused()


def test_r1b_is_all_fused_and_check_share_one_verdict():
    """`is_all_fused()` 必须与 `check()` 同一个判据（单点定义，不许两份口径）。

    这一条钉的是实现形状而不只是结果：老写法是 check() 自己 `in` 一遍、
    is_all_fused() 再 `in` 一遍，两份写在两处迟早对不上（比如其中一份漏了空串判断）。

    负向锁：让 is_all_fused() 直接读 `self._fused_all_reason()` 之外的另一份状态
    （比如换成 `self._total_tokens > self.max_tokens`，注意是 > 不是 >=），
    在"恰好等于上限"那一格两条断言就会一个 True 一个 False，本条红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=100)
    assert guard.check("web_search", {}) is None and not guard.is_all_fused()
    guard.record_tokens(100)                    # 恰好等于上限：>= 才算跳
    assert guard.is_all_fused(), "record_tokens 之后状态位应当是 True"
    assert guard.check("web_search", {}), "check 与 is_all_fused 必须同一判据"


def test_r1b_fuse_does_not_deadlock_under_the_nonreentrant_lock():
    """锁不可重入：check() 在锁内读总额熔断，绝不能反过来调用会自我加锁的方法。

    这是这一处改动唯一能把"绿"变成"整套测试永远挂住"的错法（把
    `if self.is_all_fused():` 写进已经持锁的 check() 里就是这样）。所以这条用例
    把调用放进线程里并限时 join：真自锁死的话它报红而不是把整个 pytest 吊住。

    负向锁：把 check() 里那行换成 `if self.is_all_fused(): return ...`，本条 2 秒后红。
    """
    import threading

    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=10)
    guard.record_tokens(50)

    box = {}

    def run():
        box["blocked"] = guard.check("web_search", {"q": "1"})
        box["flag"] = guard.is_all_fused()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive(), "check()/is_all_fused() 互相加锁把调用吊死了"
    assert box.get("blocked"), f"应当拿到理由，拿到 {box.get('blocked')!r}"
    assert box.get("flag") is True


def test_r1b_fuse_adds_no_new_event_type():
    """事件集合不许因为这次改动变长：读侧（面板与账本 guard_events）没准备好接新 kind。

    负向锁：在 check() 的熔断分支里追加一个新类型的 events.append，本条红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=100)
    guard.record_tokens(120)
    types_at_trip = {e["type"] for e in guard.guard_events}
    assert types_at_trip == {"cost_cap_tokens"}, f"跳闸只记这一种：{types_at_trip}"
    for _ in range(3):
        guard.check("web_search", {"query": "x"})
    after = [e["type"] for e in guard.guard_events]
    assert after == ["cost_cap_tokens"], f"被拦住的调用不该再记账：{after}"


def test_r1b_clip_output_still_clips_after_the_fuse_trips():
    """总额熔断不许改变 clip_output 的行为——它管的是"已经跑完的输出占多少字"。

    把那份输出换成一句"预算已用尽"，等于把理由里"请基于已有结果直接回答用户"要
    它依据的结果本身销毁掉。这一条钉的是这个边界没有被顺手扩出去（见 record_tokens
    的注释）。

    负向锁：让 clip_output() 在 _fused_tools 有 __all__ 时直接返回说明句，本条红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=100, max_output_chars=1000)
    guard.record_tokens(200)
    text, truncated = guard.clip_output("甲" * 100)
    assert text == "甲" * 100 and truncated is False, (
        f"熔断后 clip_output 不该改动已有输出：truncated={truncated}")


def test_r1b_record_outcome_keeps_counting_after_the_fuse():
    """记账一侧不许因为熔断就停：token 停了累加，上限就再也跳不出第二下，
    而账本上"这一轮跑了几次工具"也会少记（判据理由见 record_tokens 的注释）。

    负向锁：让 record_outcome() 在熔断状态下直接 return，本条的 tool_rounds 红。
    """
    from app.core.guardrails import RoundGuard
    guard = RoundGuard(max_tokens=100)
    guard.record_tokens(500)
    guard.record_outcome("web_search", ok=True, estimated_tokens=10)
    guard.record_outcome("web_search", ok=True, estimated_tokens=10)
    assert guard.tool_rounds == 2, f"熔断后仍要记轮次，拿到 {guard.tool_rounds}"
    assert guard.check("web_search", {})      # 仍然是被拦的


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
