"""v0.35.0 工单 #78：**首次挂起也要付频控预算**。

#79 只钉了"声明 `suspends` 的工具必须在 `_FREQ_LIMITS` 里有一档"（静态配置），
但"这一档真的对一次挂起计了数"此前没有专属的锁。缺了它，把 `_freq_gate` 挪到
`func()` 之后、或给挂起加一条"先放行"的分支，配置锁照样全绿，而现网症状是：
一个挂起的工具每次重试都免费——挂起这一族天生招重试（用户没答、页面刷新、模型
再试一次），免费重试就是这版想关掉的那条循环面。

本文件只钉 `plan_add` 这一条**真会挂起**的工具，四格：

1. 首次挂起**计且只计一次**（账本长度 0 → 1，时间戳就是这次派发）；
2. 窗口内第二次派发被现有闸门拒掉，且压根没进函数体（`tool_ran=False`）；
3. 窗口过后，同样的重试重新被允许（继续挂起，而不是被永久饿死）；
4. 窗口过后，**已经停在断点上的那枚答案**仍递得进去，日程真的落库——频控挡住的是
   重试，不该把一条合法续跑永久锁死。

2/4 的"拒"是把 `_FREQ_LIMITS["plan_add"]` 临时改成 cap=1 观察出来的：默认档是
6（由 `test_v0350_freq_suspends_locks.py` 钉着），在 cap=6 下"第二次"本来就不该
被拒。测试改的是**表里的数**，不是运行时逻辑；窗口长度仍从表里现读，不在测试里
抄第二份。所有时钟都换成了可控的 `monotonic`，不 sleep、不依赖用例跑多快。
"""
from collections import defaultdict
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import app.tools.builtin_tools  # noqa: F401  导入期注册 plan_add：不导入就没有真工具
from app.core import schedule
from app.tools import executor as ex


@pytest.fixture(autouse=True)
def _fresh_freq_ledger(monkeypatch):
    """频控账本是进程级全局：每条用例一份，否则前一条的计数会带进后一条。"""
    monkeypatch.setattr(ex, "_FREQ_LEDGER", defaultdict(list))


def _clock(monkeypatch, start: float = 1000.0) -> SimpleNamespace:
    """把执行器里的 `time.monotonic()` 换成手拨的钟（不 sleep）。"""
    clock = SimpleNamespace(now=start)
    monkeypatch.setattr(ex, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def _cap_at_one(monkeypatch) -> float:
    """把 plan_add 的档临时改成 cap=1，窗口仍取表里拍板的那一个。

    cap=1 让"一次挂起吃掉一格"在两行内可见；默认 cap=6 时第二次派发本来就该放行，
    拿默认档断言"被拒"会把一条正确的实现判红。窗口不写死：改了窗口，这条跟着走。
    """
    window, _cap = ex._FREQ_LIMITS["plan_add"]
    monkeypatch.setitem(ex._FREQ_LIMITS, "plan_add", (window, 1))
    return window


def _next_saturday() -> str:
    """「下周六」的落点：下周一 + 5 天。与 `_day_from_text` 的换算同一口径。"""
    base = date.fromisoformat(schedule.today())
    monday = base - timedelta(days=base.weekday())
    return (monday + timedelta(days=7 + 5)).isoformat()


def _ledger(user_id: str) -> list:
    return ex._FREQ_LEDGER[("plan_add", user_id)]


def test_the_first_suspension_consumes_exactly_one_slot(isolated_schedule, monkeypatch):
    """挂起不是"没产出的失败"，但它确实派发过一次：账本必须 0 → 1。

    第二次挂起同样计一次（顺手钉"不是只给首跑记一笔"）；时间戳取的是派发那一刻，
    不是函数返回之后——否则一次慢工具会把自己的窗口起点推迟。
    """
    clock = _clock(monkeypatch)
    user = "u-t78-first"
    assert _ledger(user) == [], "账本没清干净，这条锁等于没跑"

    first = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert first.suspended is True and first.ok is False, first.text
    assert first.tool_ran is True, "挂起是从函数体里抛出来的，答案确实递进去过"
    assert len(_ledger(user)) == 1, "首次挂起没付频控预算：重试这一族从此免费"
    assert _ledger(user)[0] == clock.now, "计的不是派发那一刻"

    second = ex.execute_tool_detailed("plan_add", {"text": "下个月交房租"}, user_id=user)
    assert second.suspended is True, second.text
    assert len(_ledger(user)) == 2, "只有首跑计次：第二次挂起又白嫖了一格"
    assert schedule.plan(user) == [], "两次都该停在断点上，一条日程都不许先落"


def test_a_second_dispatch_inside_the_window_is_refused(isolated_schedule, monkeypatch):
    """窗口内的第二次派发被现有闸门挡下，而且没进函数体。

    反控在末尾：把 `_freq_gate` 摘掉之后同一发会被放行——证明上面那句"被拒"确实
    来自频控，而不是"plan_add 自己又挂了"或别的偶然。
    """
    _clock(monkeypatch)
    _cap_at_one(monkeypatch)
    user = "u-t78-refuse"

    first = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert first.suspended is True, first.text

    blocked = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert blocked.ok is False and blocked.suspended is False, blocked.text
    assert blocked.tool_ran is False, "被频控挡下的调用已经进了函数体"
    assert "过于频繁" in blocked.text and "还需等待" in blocked.text, blocked.text
    assert schedule.plan(user) == [], "被挡下的调用写进了日程"

    monkeypatch.setattr(ex, "_freq_gate", lambda tool_name, user_id: None)
    unblocked = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert unblocked.suspended is True, \
        f"反控失败：摘掉闸门后第二次派发仍被拒，拒绝不是频控给的：{unblocked.text}"


def test_a_retry_after_the_window_is_allowed_again(isolated_schedule, monkeypatch):
    """窗口过后同一发重试重新被允许：频控是"等一等"，不是"这条工具今天别用了"。

    时钟推进 `window + 1` 秒（窗口判据是 `now - t < window`，卡在等号上不好读），
    账本里旧的那一格必须已经过期、新的一格落在新时刻上。
    """
    clock = _clock(monkeypatch)
    window = _cap_at_one(monkeypatch)
    user = "u-t78-retry"

    first = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert first.suspended is True, first.text
    refused = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert refused.ok is False, refused.text

    clock.now += window + 1
    retried = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert retried.suspended is True and retried.ok is False, retried.text
    assert retried.tool_ran is True, "窗口过后的重试没进函数体"
    assert "过于频繁" not in retried.text, retried.text
    assert _ledger(user) == [clock.now], f"旧窗口没退出账本：{_ledger(user)}"


def test_a_parked_answer_still_resumes_after_the_window(isolated_schedule, monkeypatch):
    """停在断点上的那枚答案是"合法续跑"，不是刷接口：窗口过后必须递得进去。

    这一格与上一条分开，因为产品含义不同：上一条是用户重新发起，这一条是用户终于
    回答了 5 分钟前弹出的问题。后者被频控永久锁死 = 挂起这一版的意义就没了。
    """
    clock = _clock(monkeypatch)
    window = _cap_at_one(monkeypatch)
    user = "u-t78-resume"

    first = ex.execute_tool_detailed("plan_add", {"text": "周末买菜"}, user_id=user)
    assert first.suspended is True, first.text
    packet = {"state": first.suspension["resume_state"], "answer": "下周六"}

    immediate = ex.execute_tool_detailed(
        "plan_add", {"text": "周末买菜"}, user_id=user, resume=packet)
    assert immediate.ok is False and immediate.tool_ran is False, immediate.text
    assert "过于频繁" in immediate.text, immediate.text
    assert schedule.plan(user) == [], "被挡下的续跑已经落了一条日程"

    clock.now += window + 1
    resumed = ex.execute_tool_detailed(
        "plan_add", {"text": "周末买菜"}, user_id=user, resume=packet)
    assert resumed.ok is True and resumed.suspended is False, resumed.text
    rows = schedule.plan(user, _next_saturday())
    assert [r["text"] for r in rows] == ["周末买菜"], rows
    assert schedule.plan(user, schedule.today()) == [], "回答给了日子却还落在今天"
    assert len(_ledger(user)) == 1, "窗口过后这次续跑没记账（或旧账没退出）"
