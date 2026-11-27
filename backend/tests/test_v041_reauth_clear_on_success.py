"""#283：二次验证成功后要清本人的失败预算（2026-11-27 收口）。

现场：两道闸都在**验口令之前**判——`admin_reauth_endpoint` 先 `_admin_reauth_throttled`，
满了直接 429，根本走不到 `verify_password`。于是 ② 号那道（按 user_id 计数、30 次/10
分钟、跨来源）一旦被别人从任意 IP 打满，**这位管理员本人在自己机器上、拿着正确口令也
进不去**——他连「证明我是我」的机会都没有，只能等窗口过去。这就是「已持会话者可跨来源
把另一个管理员锁在二次验证外」。

与「任何成功都不还回预算」（F3）的关系：那条守的是**免凭据**的登录/注册端点，在那里
「成功」随手可得（猜对自己的口令、再注册个小号），成功还预算会让枚举无限续命。这里正相反
——端点前面站着已鉴权 + role==admin 两道门，能走到验证成功的人必须先是持票管理员本人。
登录/注册那两本账的判据一个字没动（下面第三条反面用例钉着）。
"""
import pytest

from app.core import auth_router as ar

VICTIM = "u_admin_victim"


@pytest.fixture(autouse=True)
def _clean_ledgers():
    ar.reset_admin_reauth_throttle_for_tests()
    yield
    ar.reset_admin_reauth_throttle_for_tests()


def _fill_from_foreign_sources(user_id: str, count: int = 35) -> None:
    """模拟「别人从很多不同来源打同一个账号」：② 号那道按 user_id 计数，30 次就满。"""
    for i in range(count):
        ar._note_admin_reauth_failure(user_id, "203.0.113.%d" % i)


def test_a_victim_is_locked_out_before_fixing():
    """前提自证：不清账时，连本人从没见过的来源也进不去（这就是待修的坑）。"""
    _fill_from_foreign_sources(VICTIM)
    assert ar._admin_reauth_throttled(VICTIM, "198.51.100.9") is True, \
        "前提塌了：这条用例本来要复现「被他人打满」"


def test_a_successful_reauth_clears_the_victims_own_budget():
    _fill_from_foreign_sources(VICTIM)
    assert ar._admin_reauth_throttled(VICTIM, "198.51.100.9") is True
    # 本人从自己的来源验证成功
    ar._clear_admin_reauth_failures(VICTIM, "198.51.100.9")
    assert ar._admin_reauth_throttled(VICTIM, "198.51.100.9") is False, \
        "验证成功后本人仍被锁在二次验证外——#283 没修好"


def test_clearing_one_admin_does_not_touch_another():
    """清的是**本人**那一格，不是把账本一把清空。"""
    _fill_from_foreign_sources(VICTIM)
    ar._note_admin_reauth_failure("u_other_admin", "203.0.113.1")
    ar._clear_admin_reauth_failures(VICTIM, "198.51.100.9")
    assert ar._ADMIN_REAUTH_FAILS_BY_USER.get("u_other_admin"), \
        "别人那格被顺手清了：清账必须是「只清本人」"


def test_login_failure_budget_is_still_never_paid_back():
    """F3 那条裁决没被推翻：登录那本账不看 reauth 的成功。"""
    assert not hasattr(ar, "_clear_failures_for_user"), \
        "登录失败账不该有按人清的入口——那正是 F3 禁止的形状"
    for i in range(5):
        ar._note(ar._FAILS, "203.0.113.200")
    ar._clear_admin_reauth_failures("u_someone", "203.0.113.200")
    assert ar._recent(ar._FAILS, "203.0.113.200", ar.FAILURE_WINDOW_SECONDS, ar._now()), \
        "reauth 清账把登录失败账也清了——越界了"
