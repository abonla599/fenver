"""v0.35 工单 #29 负向契约锁：护栏的第三态（挂起）既不进失败退避，也不是"没记过账"。

缺陷：v0.35 A3 给工具结果加了第三格 `ToolOutcome.suspended`（停在断点问用户），
但 `backend/app/core/guardrails.py:137-185` 的 `record_outcome` 只有成功/失败两态，
读侧把挂起读成 `ok=False` 就记一次**失败**，而失败退避的算术（`guardrails.py:175-185`，
加分与跳闸在 `:177-180`；阈值常量 `:31` CONSECUTIVE_FAIL_THRESHOLD=2，写熔断表在 `:183`）
两次就把这个工具在本轮熔断——用户两次"请把验证码发我"之后再也续不上。
（以上坐标是 #76a 实读更正过的：#29 落笔时引用的是改动前的行号，早就腐烂了。）

本票只做两件事：护栏里那一格第三态的**状态**，以及下面这些**会红的锁**。
调用点是 **工单 #76** 的活：第一波 **#76a 已落**——两条聊天路径（读数点 pipeline.py:306、
streaming.py:478）、react_agent.py:212 与 task_agent.py:178 都改吃 execute_tool_detailed
并传 `suspended=bool(outcome.suspended)`；判据读的是结构化那一格（挂起在 executor.py:181、
成败在 :197，#76b 已把 react_agent 的成败判据从 `startswith("✓")` 换成 `outcome.ok`），不是
文本前缀。现网行为仍然一字未变，但**原因换了**：不再是"调用点
没接（休眠）"，而是 `backend/app/**` 里 `ToolSuspended` 零 raise、registry 无一声明
`suspends`，第三态根本到不了。A4 交出第一条真 `suspends: True` 的工具之前 #76 必须落全。
因此本文件里任何一条锁都**不依赖**调用点是否已经传 `suspended=`——它们直接对 RoundGuard 说话；
依赖调用点的那两把行为锁在 `tests/test_v0350_76a_suspend_consumer_third_state.py`（#76a 补）。

纪律（今天踩过的三个坑，写在脸上）：
* 判据一律问机器——`check()` 在门口给的回话、公开属性 `tool_rounds` /
  `guard_events` / `suspension_counts`；不由被测对象自报，也没有 mock 替实现说话。
* 没有任何一条锁把"改前那份"锚在 `HEAD` 上（提交那一刻它就自毁并反向变红）。
  反例改用**同侧对照**：(fail, fail) 与 (fail, suspend, fail) 必须落到同一个熔断状态，
  两条断言都不需要历史副本。
* 不 assert 环境变量，所以没有"fixture 局部变量冒充 os.environ"的空锁可言。

跨请求：`RoundGuard` 是**单轮**状态（`guardrails.py:49` "每条聊天路径在一次用户请求中
创建一个实例"；四个实例化点 `pipeline.py:209`、`streaming.py:210`、`react_agent.py:84`、
`task_agent.py:125`），本模块无模块级可变量，挂起这一格不跨请求传播 →
不需要 `httpx.ASGITransport` + `asyncio.gather` 那一层。频控账本
（`tools/executor.py` 的 `_FREQ_LEDGER`，那才是跨请求的）本票没碰。
"""
import inspect


# 每次只隔离一个变量：挂起这把锁要判的是"失败退避不跳"，所以把另外两条会拦人的
# 闸门（重复检测 ≥3 次同参、轮次上限 5）抬到够不着的地方，否则红了分不清是谁干的。
ISOLATED = dict(fail_threshold=2, duplicate_threshold=99, max_tool_rounds=99)


def _guard(**kw):
    opts = dict(ISOLATED)
    opts.update(kw)
    from app.core.guardrails import RoundGuard
    return RoundGuard(**opts)


# ---------- 锁 (i)：N 次挂起在一轮之内绝不熔断这个工具 ----------

def test_29_lock_i_suspensions_never_fuse_the_tool_in_one_round():
    """八次挂起之后这个工具照样放行——修好之后的核心行为。

    传的是 `ok=False, suspended=True`：A3 的挂起在 `ToolOutcome` 里 `ok` 就是 False
    （`executor.py:197` 读 `response.success`），所以这一路正是缺陷现场的那一次调用。

    删掉这把锁会红什么：把 `record_outcome` 里 `suspended` 的分支删掉（挂起顺流成失败）
    → 第 2 次挂起就跳退避 → 循环里 `i=1` 那句 `check()` 拿到熔断理由，本条红。
    """
    guard = _guard()
    for i in range(8):
        blocked = guard.check("sms_code", {"q": i})
        assert blocked is None, f"第 {i + 1} 次挂起后工具被熔断：{blocked!r}"
        guard.record_outcome("sms_code", ok=False, suspended=True)
    assert guard.tool_rounds == 8, (
        f"挂起那一轮必须照记（不记就是给重试留免费额度），拿到 {guard.tool_rounds}")
    assert not guard.is_all_fused(), "挂起不该碰总额熔断"
    assert guard.check("sms_code", {"q": "最后一次"}) is None


# ---------- 锁 (ii)：真失败两次仍然照今天一样熔断（反方向对照） ----------

def test_29_lock_ii_real_failures_still_fuse_exactly_as_today():
    """(ii) 这一把证明"没松任何东西"：真失败的算术、阈值、文案一字未动。

    与 (i) 同参数、同工具名，唯一差别就是传不传 `suspended=`——所以两条一起绿才叫
    "第三态是中性的"，单独绿哪一条都不算。

    删掉这把锁会红什么：把 `guardrails.py:175-185` 的失败退避删掉（过度放宽方向）
    → 第二次失败后 `check()` 返回 None，本条红；把阈值常量 2 改成 3 也会红。
    """
    guard = _guard()
    guard.record_outcome("sms_code", ok=False)
    assert guard.check("sms_code", {"q": 1}) is None, "第一次失败就熔断，太紧"
    guard.record_outcome("sms_code", ok=False)
    blocked = guard.check("sms_code", {"q": 2})
    assert blocked, "两次连续失败必须熔断——这是现网行为，本票不许放宽"
    assert "失败" in blocked, f"熔断文案要说明原因：{blocked!r}"
    assert "连续失败 2 次" in blocked, f"阈值算术应当还是 2：{blocked!r}"
    assert {"fail_backoff"} <= {e["type"] for e in guard.guard_events}


# ---------- 锁 (iii)：`__all__` 总额熔断照旧挡下一切，与挂起无关 ----------

def test_29_lock_iii_all_fuse_blocks_everything_regardless_of_suspensions():
    """(iii) 读 `__all__` 的那条路（`check()` def 在 `:89`、门口那三行在 `:106-108`
    → `_fused_all_reason()` 在 `:220-228`）
    不许因为新增第三态而被绕开或顺手扩出去。

    删掉这把锁会红什么：
    * 把 `check()` 里 `fused_all` 那三行删掉 → 第一个 `check` 返回 None，本条红；
    * 让挂起分支去 `self._fused_tools.pop("__all__")`（"挂起就把闸松了"）→
      `is_all_fused()` 那句红。
    """
    guard = _guard(max_tokens=100)
    guard.record_tokens(120)
    for i in range(4):
        guard.record_outcome("sms_code", ok=False, suspended=True)
    assert guard.is_all_fused(), "挂起不许消掉总额熔断状态"
    first = guard.check("sms_code", {"q": 1})
    assert first, "总额熔断跳了却放行 = v0.34.2 修掉的那个洞复活"
    assert "token" in first or "上限" in first, f"理由要指向总额上限：{first!r}"
    assert guard.check("calculator", {"expr": "1+1"}) == first, (
        "总额熔断对所有工具应当给同一句话——包括这一轮挂过起的 sms_code")


# ---------- 锁 (iv)：挂起仍然被记下来（可观测） ----------

def test_29_lock_iv_suspension_is_still_recorded_and_observable():
    """(iv) 中性 ≠ 隐形。挂起要有一格**自己的**账：独立计数器 + 独立事件类型。

    为什么是独立计数器、不复用 `_consecutive_fails`：那格是熔断算术的输入
    （`guardrails.py:180` 直接拿它跟阈值比），往里记挂起等于锁 (i) 当场失效；而如果为了"不计熔断"
    给它加一个平行阈值，算术就从一处变成两处——本票要求除 (i)(iv) 之外一切照旧。
    事件类型另起 `suspend` 也是同一理由：读侧（面板/账本）看到 `fail_backoff`
    就会以为工具坏了，而挂起恰恰是"在等人"。

    删掉这把锁会红什么：把挂起分支里的 `suspension_counts` 累加或 `events.append`
    删掉（"既然不计熔断就干脆什么都不记"）→ 两句断言分别红。
    """
    guard = _guard()
    guard.record_outcome("sms_code", ok=False, suspended=True)
    guard.record_outcome("sms_code", ok=False, suspended=True)
    guard.record_outcome("web_search", ok=False, suspended=True)
    assert guard.suspension_counts == {"sms_code": 2, "web_search": 1}, (
        f"挂起要有自己的计数器：{guard.suspension_counts!r}")
    events = guard.guard_events
    assert [e["type"] for e in events] == ["suspend"] * 3, events
    assert events[0]["tool"] == "sms_code" and events[0]["suspensions"] == 1, events[0]
    assert events[1]["suspensions"] == 2, f"计数要按工具各自累：{events[1]}"
    assert "fail_backoff" not in [e["type"] for e in events], "挂起不许冒充失败退避"
    assert guard.tool_rounds == 3, "挂起照记轮次（成本上限那条口径不变）"


# ---------- 锁 (v)：挂起是中性的——既不加分也不抹历史 ----------

def test_29_lock_v_fail_suspend_fail_lands_on_the_same_fuse_state_as_fail_fail():
    """(v) 三条路同问一台机器：(fail, fail) 与 (fail, suspend, fail) 必须给出
    **同一句**熔断理由；(suspend, suspend) 则一句都不给。

    这一把同时钉住两个反方向，而且不需要"改前那份"当参照：
    * 挂起**抹历史**（把它当成功、清 `_consecutive_fails`）→ 第二条不熔断 → `state_b`
      是 None → 红；
    * 挂起**加历史**（顺流成失败）→ (fail, suspend) 就跳闸，随后理由变成"连续失败 3 次"
      → `state_a != state_b` → 红。

    删掉这把锁会红什么：上面两种改法任挑一种，本条红；钉"成功清零"那把老锁也仍然绿——
    清零只对 `ok=True` 成立（代码行是 `guardrails.py:176`），挂起不是成功。
    **（#76a 更正）**原文这里写的是 `guardrails.py:98`，指错了对象：那把"成功清零"的老锁
    是**测试文件**里的 `tests/test_v031_trusted_execution.py:98
    test_r1_fail_backoff_resets_on_success`，而 `guardrails.py:98` 现在只是 `check()` 里的
    `with self._lock:`。按实读把"测试锁的坐标"和"它守的代码行坐标"分开写，不写成同一个。
    """
    a = _guard()
    a.record_outcome("sms_code", ok=False)
    a.record_outcome("sms_code", ok=False)
    state_a = a.check("sms_code", {"probe": "x"})

    b = _guard()
    b.record_outcome("sms_code", ok=False)
    b.record_outcome("sms_code", ok=False, suspended=True)
    b.record_outcome("sms_code", ok=False)
    state_b = b.check("sms_code", {"probe": "x"})

    assert state_a, f"对照组本该熔断：{state_a!r}"
    assert state_b == state_a, (
        f"挂起改动了失败历史：{state_b!r} != {state_a!r}")
    assert b.suspension_counts == {"sms_code": 1}, b.suspension_counts

    c = _guard()
    c.record_outcome("sms_code", ok=False, suspended=True)
    c.record_outcome("sms_code", ok=False, suspended=True)
    assert c.check("sms_code", {"probe": "y"}) is None, "两次挂起不该攒出一次熔断"


# ---------- 公开 API 向后兼容：#76a 之后仍在用的两参老写法不许被迫改 ----------

def test_29_old_ok_fail_callers_keep_working_unchanged():
    """#76a 接上了四个调用点，但两参老写法还在（react_agent 的兜底异常支），原意不许变。

    `suspended` 做成**关键字限定**参数（默认 False）而不是第 3 个位置参数，理由：
    位置参数会顶掉 `estimated_tokens` 的位置——`react_agent.py:217` 那条兜底异常支
    `record_outcome(tool_name, False)` 不疼，但任何按位置传第三参的写法会静默把
    token 数读成布尔。默认参数 vs 新关键字里选后者，因为它的破坏面可被签名检查证明为零。
    另：把 `ok` 改成 `Optional[bool]`（None=挂起）被否掉——现网没人传 None，
    而 `if ok:` 这种读法会把 None 静默折成失败，正是本票要修的错法。

    删掉这把锁会红什么：把 `suspended` 挪成第 3 个位置参数 → `kind is
    KEYWORD_ONLY` 红；让老的两参/三参写法不再跳退避 → 行为断言红。
    """
    from app.core.guardrails import RoundGuard
    params = inspect.signature(RoundGuard.record_outcome).parameters
    assert params["suspended"].kind is inspect.Parameter.KEYWORD_ONLY, params
    assert params["suspended"].default is False, "默认值必须是不挂起（False）"
    assert list(params) == ["self", "tool_name", "ok", "estimated_tokens", "suspended"], list(params)

    guard = RoundGuard()
    guard.record_outcome("web_search", False)     # 位置写法（react_agent.py:217 兜底异常支）
    guard.record_outcome("web_search", ok=False, estimated_tokens=7)  # 关键字写法
    blocked = guard.check("web_search", {"query": "同一个参数"})
    assert blocked, "两态老写法仍须在两次失败后熔断"
    assert guard.tool_rounds == 2
