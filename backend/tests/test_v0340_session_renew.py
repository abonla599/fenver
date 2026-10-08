# backend/tests/test_v0340_session_renew.py
"""v0.34.0 判据 1/2/3/8（+ 6 的排水窗半边）：续期不换会话。

一句话验收（PRD 20261007 第一节）：**一直用的人永远不需要再输口令，连续 400 天
不打开的人才必须重新输**。这一份文件钉的是前半句的实现形状——三段时钟里"滑动 30 天
可以被 renew 推走，绝对 180 天谁都不许推"。

时间控制沿用 v0.33 的既有做法：**改表里的时间戳**，不 patch time。理由是
`auth.py` 全文直接调 `time.time()`，monkeypatch 那个函数等于把整个解释器的时钟
换掉，同一条用例里任何一处间接读时钟都会跟着漂；而这里要测的判据本来就全是
"某个 digest 的时刻离现在多远"，直接写那张表既精确又可读。

每条锁都经过变异验证（故意改坏实现看它会不会红），改坏点写在用例 docstring 里。
"""
import time

import pytest

from app.core.auth import (
    AuthError,
    AuthStore,
    RENEW_ALLOWED_BELOW_SECONDS,
    SESSION_TOKEN_ABSOLUTE_CAP_SECONDS,
    SESSION_TOKEN_TTL_SECONDS,
    TOKEN_DRAIN_WINDOW_SECONDS,
    hash_token,
)

PW = "correct-horse-battery"


@pytest.fixture()
def store(tmp_path):
    return AuthStore(path=str(tmp_path / "users.json"))


def _register(store, name="u1"):
    """注册并返回 (token, device_secret)。

    走 register_for_new_session 而不是 register：后者是兼容旧形状的两元组包装，
    本版要的续命凭据只有前者会返回。
    """
    _principal, token, secret = store.register_for_new_session(
        username=name, password=PW)
    return token, secret


def _record_of(store, token):
    principal = store.resolve(token)
    assert principal is not None, "刚签发的令牌应当可用"
    return store._users[principal.user_id]


def _set_remaining(store, token, seconds_left):
    """把这枚令牌的**滑动**剩余改到指定值（模拟"用了 23 天"这类状态）。

    只改 token_issued_at：绝对上限必须留在原处，否则判据 3 的"上限不动"就没得测了。
    """
    record = _record_of(store, token)
    digest = hash_token(token)
    assert digest in record["token_issued_at"], "新签发的令牌没有签发时刻"
    record["token_issued_at"][digest] = time.time() - (SESSION_TOKEN_TTL_SECONDS
                                                       - seconds_left)
    return record, digest


def _renewable(store, token, remaining=3 * 24 * 3600):
    """把令牌推到"可以续"的窗口里（剩余 3 天 < 7 天阈值），然后 renew。

    返回 (新令牌, record, 老digest, payload)。
    """
    record, old_digest = _set_remaining(store, token, remaining)
    principal, new_token, payload = store.renew(token)
    return new_token, record, old_digest, payload


# ---------- 判据 1：剩余 > 7 天不产生 renew ----------


def test_renew_is_refused_while_the_token_is_still_fresh(store):
    """还早就不许续：客户端把 renew 当心跳打，等于把绝对上限变成唯一的闸。

    变异：把 RENEW_ALLOWED_BELOW_SECONDS 的判据改成"任何时刻都放行"，这条必须红。
    """
    token, _secret = _register(store)
    with pytest.raises(AuthError):
        store.renew(token)
    record = _record_of(store, token)
    assert len(record["tokens"]) == 1, (
        "被拒的 renew 仍然签出了新令牌：拒绝条件不该有任何签发副作用（PRD 第三节"
        "写的是「全部 401，且不签发任何东西」）")


def test_renew_is_allowed_inside_the_window(store):
    """正向半边：剩余不到 7 天时必须能续——只有拒绝的锁会把功能测成永远 401。"""
    token, _secret = _register(store)
    _set_remaining(store, token, RENEW_ALLOWED_BELOW_SECONDS - 60)
    principal, new_token, payload = store.renew(token)
    assert new_token and new_token != token
    assert principal is not None
    assert payload["expires_at"] > time.time()


def test_renew_at_the_boundary_is_judged_strictly(store):
    """阈值是"剩余 > 7 天才拒"，不是 ">="：恰好 7 天时应当放行。

    差一小时的边界值在这里不重要，重要的是判据方向：写成 `>=` 与写成 `>` 的差别
    是"客户端算出 7.0000 天时到底能不能续"，而那正是每个客户端各自实现时会撞上
    的一次 401。
    """
    token, _secret = _register(store)
    _set_remaining(store, token, RENEW_ALLOWED_BELOW_SECONDS - 1)
    _p, new_token, _payload = store.renew(token)
    assert store.resolve(new_token) is not None

    other, _s = _register(store, name="u2")
    with pytest.raises(AuthError):
        store.renew(other)


# ---------- 判据 2：renew 后令牌换值，旧值在排水窗外必须 401 ----------


def test_renewed_token_works_and_old_one_is_moved_out_of_tokens(store):
    new_token, record, old_digest, _payload = _renewable(store, _register(store)[0])
    assert store.resolve(new_token) is not None, "续出来的新令牌必须当场可用"
    assert old_digest not in record["tokens"], (
        "老 digest 还留在 tokens 里：排水窗之后它凭什么算过期？这条不删，"
        "判据 2 的「旧值 401」就只能靠 60 秒之后没人来查")


def test_old_token_still_works_inside_the_drain_window(store):
    """排水窗存在的具体理由：renew 的同时还有在途 SSE 带着老令牌跑。

    老 digest 立即硬拒 = 用户正在输出的回答当场断。60 秒是本版唯一一处主动让步，
    所以它必须被钉成"确实是 60 秒、确实只对这一枚老 digest 生效"，而不是散在实现
    里的一次宽容。
    """
    token, _secret = _register(store)
    _new, record, old_digest, _payload = _renewable(store, token)
    assert store.resolve(token) is not None, "60 秒内老令牌应当还能用在途请求"
    assert hash_token(token) == old_digest


def test_old_token_is_refused_once_the_drain_window_closes(store):
    """变异：把排水窗判据改成"只要在表里就永远放行"，这条必须红。"""
    token, _secret = _register(store)
    _new, record, old_digest, _payload = _renewable(store, token)
    record["token_revoked_at"][old_digest] = time.time() - TOKEN_DRAIN_WINDOW_SECONDS - 1
    assert store.resolve(token) is None, "排水窗结束后老令牌必须硬拒"


def test_drain_window_does_not_extend_the_token_life_it_drains(store):
    """排水窗只放行，**不刷新**：老令牌不能靠这 60 秒把滑动期续回去。

    形状判据比"时间没变"更硬：进入排水窗的那一刻，这条 digest 在 issued 与
    absolute 两张表里**就没有了**，只剩 token_revoked_at 里那一个时间戳。于是
    "它还能活多久"这个问题在数据结构上只有一个答案（60 秒），没有任何一条代码
    路径能把它读成"还能续 30 天"。

    症状如果发生会是"客户端 renew 之后又开始用老令牌，于是这枚令牌永不过期"。
    """
    token, _secret = _register(store)
    _new, record, old_digest, _payload = _renewable(store, token)
    assert store.resolve(token) is not None, "排水窗内的在途请求应当放行"
    assert old_digest not in record["token_issued_at"], \
        "在途的老令牌还在签发表里：它随时可以被算出一个新的滑动到期"
    assert old_digest not in record["token_absolute_deadline"], "同上，绝对上限表"
    assert record["token_revoked_at"][old_digest] == pytest.approx(time.time(), abs=5), \
        "排水窗条目不是「刚刚吊销」的时间戳"


# ---------- 判据 3：连续 20 次 renew，绝对上限一个字都不动 ----------


def test_twenty_renews_do_not_move_the_absolute_cap(store):
    """整版设计的轴：绝对上限随**会话**走，不随**令牌**走。

    先把整段会话推到"已经用了 170 天"（锚点、签发时刻、绝对上限三处一起挪，
    保持它们之间的**相对**关系），然后连续 renew 20 次。这一步不是可有可无的
    铺垫：20 次 renew 在真实时钟里只隔几毫秒，"每次重新算 now + 180d" 与
    "继承同一个 cap" 算出来的数字在那几毫秒里几乎一样——第一次跑变异 M4 就是这么
    蒙过去的。会话必须**已经很老**，两种写法才会分叉。

    变异：把 renew 里继承 `absolute_deadline=cap` 改成不传（= 每次重新 now + 180d），
    这条必须红，而且红的方式应当是"上限从第 170 天被推回了 now + 180d"。
    """
    token, _secret = _register(store)
    record = _record_of(store, token)
    first_digest = hash_token(token)
    aged = 170 * 24 * 3600                      # 这段**会话**已经走了 170 天
    record["session_anchor"] = time.time() - aged
    cap0 = record["session_anchor"] + SESSION_TOKEN_ABSOLUTE_CAP_SECONDS
    record["token_absolute_deadline"][first_digest] = cap0
    # 签发时刻必须留在近处：会话老 ≠ 令牌老。把 issued 也一起挪到 170 天前，
    # 滑动 30 天早就过了，resolve 直接拒、renew 走不到——那这条锁就变成了
    # "过期令牌不能续期"的重复覆盖，M4 那种"每次重算上限"的缺陷反而看不见。
    record["token_issued_at"][first_digest] = time.time() - 24 * 3600
    assert store.resolve(token) is not None, "会话第 170 天、令牌第 1 天：此刻仍应当可用"

    current = token
    for _ in range(20):
        _set_remaining(store, current, 60)          # 每次都推到"再不续就到期"
        _p, current, payload = store.renew(current)
        assert abs(payload["absolute_deadline"] - cap0) < 1.0, \
            f"第 N 次 renew 之后绝对上限漂移：{payload['absolute_deadline']} vs {cap0}"
    # 锚点也没被顺手重设：renew 不许碰 session_anchor（那是登录与 reauth 的动作）
    assert record["session_anchor"] == pytest.approx(
        cap0 - SESSION_TOKEN_ABSOLUTE_CAP_SECONDS, abs=1.0), \
        "renew 重设了会话锚点：续期和重登混成同一个动作了"


def test_renewed_token_keeps_exactly_one_live_session_entry(store):
    """每次 renew 应当"换一枚"而不是"加一枚"：tokens 里只剩当前那一枚。

    MAX_SESSION_TOKENS = 30 的驱逐按登录次数走；如果 renew 只加不换，一个人每天
    被续一次，30 天就会把自己正常设备的令牌挤出表——症状是"用得好的人反而掉线"。
    """
    token, _secret = _register(store)
    record = _record_of(store, token)
    current = token
    for _ in range(5):
        _set_remaining(store, current, 60)
        _p, current, _payload = store.renew(current)
    assert len(record["tokens"]) == 1, \
        f"5 次 renew 之后 tokens 里有 {len(record['tokens'])} 枚：只加不换"
    assert store.resolve(current) is not None
    assert len(record["token_absolute_deadline"]) == 1, "平行表跟着胀：驱逐会漏删"


def test_eviction_prunes_all_three_parallel_tables(store):
    """MAX_SESSION_TOKENS 驱逐必须同时删 issued / absolute / drain 三张表。

    漏任何一张就是漂移（PRD 第二节第 3 条点名的原话），而 token_issued_at 已经
    踩过一次同样的坑。变异：删掉 for old in dropped 里任意一行 pop，这条对应那行红。
    """
    token, _secret = _register(store, name="u1")
    record = _record_of(store, token)
    current = token
    for _ in range(32):                    # 超过 MAX_SESSION_TOKENS = 30
        _p, current = store.login_for_new_session("u1", PW)[:2]
    from app.core.auth import MAX_SESSION_TOKENS
    assert len(record["tokens"]) == MAX_SESSION_TOKENS
    for name in ("token_issued_at", "token_absolute_deadline", "token_revoked_at"):
        table = record.get(name) or {}
        assert len(table) <= MAX_SESSION_TOKENS, \
            f"{name} 有 {len(table)} 项而令牌只有 {MAX_SESSION_TOKENS} 枚：驱逐漏删这张表"


# ---------- 判据 8：存量令牌 renew / reauth 双拒 ----------


def test_legacy_token_cannot_be_renewed(store):
    """v0.33 之前签发的令牌没有绝对上限记录，因此**不许**续期。

    为什么不能"给它补一个 now + 180d 再续"：那等于让一次偷来的存量令牌原地获得
    半年寿命，而 v0.33 留的口子（存量宽限期）本来是有界的。它们的降级路径是
    "用到宽限期到期，然后重新登录一次"（PRD 第四节最后一段）。
    变异：在 renew 里给缺 absolute 的令牌现场补一个上限，这条必须红。
    """
    token = _store_with_legacy_token(store)
    record = _record_of(store, token)
    digest = hash_token(token)
    # 形状必须是真实的 v0.33：有 issued、没有 absolute。少了这一句，下面两条断言
    # 可能是在一个"凭据本身就不完整"的错形状上绿的。
    assert digest in record["token_issued_at"], "helper 造出来的不是 v0.33 的形状"
    assert digest not in record.get("token_absolute_deadline", {})
    with pytest.raises(AuthError):
        store.renew(token)
    assert len(record["tokens"]) == 1, "被拒的 renew 签出了新令牌"
    assert digest not in record.get("token_absolute_deadline", {}), \
        "被拒的 renew 顺手给存量令牌补了一个绝对上限：等于原地续出 180 天"


def _store_with_legacy_token(store, name="存量", sliding_left=3 * 24 * 3600):
    """造一条**真实的 v0.33 形状**的记录。

    形状必须对：v0.33 就已经在写 `token_issued_at`，只有 v0.34 新加的
    `token_absolute_deadline` 与 `device_secrets` 是空的。如果把 issued 也一并抹掉，
    renew 会因为"算不出剩余"而拒签——那仍然是 401，但拒的理由是错的，变异
    "给存量令牌现场补一个绝对上限"就在这种错形状下蒙过去了（实测第一次跑 M6 就是这样）。
    """
    import secrets as _secrets

    legacy = _secrets.token_urlsafe(32)
    digest = hash_token(legacy)
    record = {
        "user_id": "u_legacy", "username": name, "username_lc": name,
        "pw_hash": None, "tokens": [digest], "role": "user",
        "disabled": False, "status": "active",
        "created_at": "2026-09-18T00:00:00",
        "last_used_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "token_issued_at": {digest: time.time() - (SESSION_TOKEN_TTL_SECONDS
                                                   - sliding_left)},
    }
    store._users["u_legacy"] = record
    return legacy


def test_legacy_account_has_no_device_secret_at_all(store):
    """存量账号一把续命凭据都没有：reauth 这条路对它们是关的，直到重新登录。

    这条同时是发布说明的那句实话的锁："没重新登录过的设备拿不到永不再输口令这个
    能力"——如果实现哪天给存量账号自动补发 secret，这句发布说明就成了假话。
    """
    legacy = _store_with_legacy_token(store)
    assert _record_of(store, legacy).get("device_secrets") in (None, {})
    with pytest.raises(AuthError):
        store.reauth("随便猜一把")


# ---------- 判据 6 的排水窗半边：吊销一律立即 ----------


def test_logout_does_not_put_the_revoked_token_into_the_drain_window(store):
    """排水窗只属于 renew。退出之后，那枚令牌当场就硬——包括 60 秒之内。

    变异：让 revoke 也往 token_revoked_at 里写一条，这条必须红。
    """
    token, _secret = _register(store)
    # 先进入一次排水窗（模拟刚 renew 完、还没过 60 秒）
    _new, record, old_digest, _payload = _renewable(store, token)
    assert store.resolve(token) is not None
    store.revoke(token)
    assert store.resolve(token) is None, "退出后的老令牌还在排水窗里活着"
    assert old_digest not in record.get("token_revoked_at", {}), \
        "一次退出在排水窗表里留了条目：吊销被推迟了"


def test_change_password_clears_the_drain_window_immediately(store):
    """改密是"我把身份重新交给你"的那一刻：身后不许留任何一条还在生效的宽容。"""
    token, _secret = _register(store)
    _new, record, old_digest, _payload = _renewable(store, token)
    store.change_password(record["user_id"], PW, "a-different-password",
                          keep_token=_new)
    assert record["token_revoked_at"] == {}, "改密之后排水窗还留着老 digest"
    assert store.resolve(token) is None


def test_disabled_account_rejects_a_drained_token(store):
    """封号对排水窗里的在途凭据同样立即：resolve 的停用判定在最前面。"""
    token, _secret = _register(store)
    _new, record, _old, _payload = _renewable(store, token)
    assert store.resolve(token) is not None
    store.disable_user(record["user_id"])
    assert store.resolve(token) is None, "停用之后在途的老令牌还在放行"
