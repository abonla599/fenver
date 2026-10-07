"""v0.33 会话token TTL：存量真实账号零失效。

此前 `auth.py` 全文无 TTL，`MAX_SESSION_TOKENS = 30` 只防表膨胀不是过期——
一枚2026-09-18 签发的令牌到2027 年仍然有效。

## 两条设计决定，各配一组负向锁

**一、签发时刻存在平行的表里，不改 `tokens` 项的形状。**
令牌项的形状被 `_token_matches` 抽象着，但 revoke / change_password /
rotate_token 三处都在按 `stored` 做列表推导——改形状要动五处。

**二、存量令牌（升级前签发的）走一个有界宽限期，且不用 `last_used_at` 判。**
`last_used_at` 是**每账号一个**，`resolve` 每命中任意一枚令牌就刷新它。一个账号
可能挂多枚令牌（真实库里有一个挂了 8 枚），用它给存量令牌背书等于"手机上活跃
就顺带让另一枚三个月没碰的令牌永不过期"——TTL 形同虚设。

所以：存量令牌比 `token_legacy_deadline`，那个值是**全账号一个固定常量**，
首次写入后永不改动。不因持续活跃而延长。
"""
import json
import time

import pytest

from app.core.auth import (
    AuthStore,
    LEGACY_TOKEN_GRACE_SECONDS,
    SESSION_TOKEN_TTL_SECONDS,
    hash_password,
    hash_token,
)


@pytest.fixture()
def store(tmp_path):
    return AuthStore(path=str(tmp_path / "users.json"))


def _register(store, name="u1", answers=None):
    """注册并返回 (principal, token)。

    真实签名见 tests/test_auth.py：register 返回二元组，且 answers 是可选的
    （真实库里就有没留找回答案的账号）。
    """
    _principal, token = store.register(
        username=name, password="correct-horse-battery", security_answers=answers)
    return token


# ---------- 判据 1：新令牌有过期时间 ----------

def test_new_token_carries_issued_at(store):
    """新签发的令牌必须带签发时刻，且落进平行的表。"""
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    assert "token_issued_at" in record, (
        "签发时刻应存在平行的表里——改tokens 项的形状要动 revoke/change_password/"
        "rotate_token 三处列表推导"
    )
    assert hash_token(token) in record["token_issued_at"]
    # tokens 本身形状不变
    assert isinstance(record["tokens"], list)
    assert all(isinstance(t, str) for t in record["tokens"]), (
        "tokens 项必须仍是纯字符串——revoke/change_password/rotate_token 都按它做推导"
    )


def test_token_expires_after_ttl(store, monkeypatch):
    """签发时刻超过 TTL 后，令牌必须解不出来。"""
    token = _register(store)
    assert store.resolve(token) is not None, "刚签发的令牌应当可用"

    # 把签发时刻推到 TTL 之外
    record = store._users[store.resolve(token).user_id]
    issued = record["token_issued_at"]
    for digest in issued:
        issued[digest] = time.time() - SESSION_TOKEN_TTL_SECONDS - 60

    assert store.resolve(token) is None, (
        f"超过 {SESSION_TOKEN_TTL_SECONDS}s 的令牌必须被拒"
    )


def test_ttl_boundary_is_not_off_by_one(store):
    """边界：TTL 之内必须仍然可用。判据要卡在两侧而不是只测一个远点。"""
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    digest = hash_token(token)
    # 距过期还有 60 秒 —— 仍在窗口内
    record["token_issued_at"][digest] = time.time() - SESSION_TOKEN_TTL_SECONDS + 60
    assert store.resolve(token) is not None, "TTL 之内必须仍然可用"


def test_broken_timestamp_is_treated_as_expired(store):
    """签发时刻坏掉按"过期"处理（fail-closed），不许 fail-open 放行。"""
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    record["token_issued_at"][hash_token(token)] = "不是时间戳"
    assert store.resolve(token) is None, (
        "坏掉的签发时刻必须判过期——鉴权读到坏数据的正确表现是拒，不是放行"
    )


# ---------- 判据 2：存量令牌零登出 ----------

def test_legacy_token_without_issued_at_still_resolves(store):
    """核心判据：存量令牌（没有签发时刻）在宽限期内必须仍能解出来。

    负向锁：把 _token_expired 里的"没有签发时刻→比 deadline"分支删掉
    （直接判过期），这条测试红。
    """
    import app.core.auth as authmod

    token = _register(store)
    uid = store.resolve(token).user_id
    record = store._users[uid]
    # 模拟升级前签发的令牌：删掉签发时刻
    del record["token_issued_at"][hash_token(token)]
    record.pop("token_legacy_deadline", None)

    assert store.resolve(token) is not None, (
        "存量令牌在宽限期内必须零失效——它们是真实库里13 个账号的 24 枚令牌"
    )
    # 且顺手钉上了 deadline
    assert "token_legacy_deadline" in record, (
        "首次命中存量令牌时应钉上宽限期deadline，"
        "写在读路径而不是登录路径——存量用户可能一直不重新登录"
    )
    assert authmod.LEGACY_TOKEN_GRACE_SECONDS > 0


def test_legacy_token_expires_after_grace_window(store):
    """宽限期过了，存量令牌也要被拒。"""
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    del record["token_issued_at"][hash_token(token)]
    record["token_legacy_deadline"] = (
        time.time() - LEGACY_TOKEN_GRACE_SECONDS - 60
    )
    assert store.resolve(token) is None, (
        "宽限期是有界的：过了就是过了"
    )


def test_legacy_deadline_is_a_calendar_moment_not_a_reference_point(store):
    """deadline 到点刚过一秒就必须失效——不许把宽限期再翻一倍。

    钉上的值是 `now + 宽限期`，也就是一个**未来时刻**。旧实现判的是
    `now - deadline > 宽限期`：减掉未来时间得到的差值要再涨满一个宽限期才成立，
    于是用户拿到的是 60 天，而 v0.33.0 的发布说明白纸黑字写的是 30 天。
    2026-10-07 真实流量第一次钉上 deadline（2026-11-06）时回读才暴露这件事。

    负向锁：把判据改回 `now - deadline > LEGACY_TOKEN_GRACE_SECONDS`，这条红
    （此刻 now-deadline 只有 1 秒，远小于 30 天，令牌会被判成还有效）。
    """
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    del record["token_issued_at"][hash_token(token)]
    record["token_legacy_deadline"] = time.time() - 1

    assert store.resolve(token) is None, (
        "deadline 就是到期日：过了一秒也算过，不再额外送一个月"
    )


def test_legacy_deadline_is_never_extended_by_activity(store):
    """**负向锁的核心**：持续活跃不得延长存量令牌的宽限期。

    这条是"不许用 last_used_at 判TTL"那句话的执行者。
    负向锁：把 _token_expired 改成"用 last_used_at 给存量令牌算"，这条红。
    """
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    del record["token_issued_at"][hash_token(token)]
    pinned = time.time() + LEGACY_TOKEN_GRACE_SECONDS
    record["token_legacy_deadline"] = pinned

    # 连续命中 50 次，每次都刷新 last_used_at（resolve 的既有行为）
    for _ in range(50):
        assert store.resolve(token) is not None

    assert record.get("token_legacy_deadline") == pinned, (
        "token_legacy_deadline 必须是一个**固定值**：持续活跃不得延长它。"
        "一旦允许延期，宽限期就变成永不收敛的口子，TTL 对存量令牌形同虚设"
    )
    # 且 last_used_at 确实被刷新了——证明这条不是"因为它没动所以没延长"
    assert record.get("last_used_at")


def test_last_used_at_is_not_used_as_ttl_judge(store):
    """源码级负向锁：_token_expired 里不许出现 last_used_at。

    运行时难验（要构造"一个账号多枚令牌、其中一枚很久没用"的场景），
    源码扫描是更直接的判据。
    """
    import inspect
    from app.core.auth import AuthStore as S

    src = inspect.getsource(S._token_expired)
    code = "\n".join(
        ln for ln in src.splitlines() if "last_used_at" not in ln and not ln.lstrip().startswith("#")
    )
    assert "last_used_at" not in code, (
        "_token_expired 不许读 last_used_at——它每账号一个、每命中任一令牌就刷新，"
        "用它判TTL 等于让一枚活跃令牌替所有令牌续命"
    )


# ---------- 判据 3：清理与内存 ---

def test_dropped_tokens_are_pruned_from_issued_table(store):
    """丢掉最老令牌时，签发表必须同步裁剪，否则无界增长。"""
    from app.core.auth import MAX_SESSION_TOKENS

    _register(store, "prune-user")
    record = store._users[next(iter(store._users))]
    for _ in range(MAX_SESSION_TOKENS + 5):
        store._issue_token(record)

    assert len(record["tokens"]) == MAX_SESSION_TOKENS
    assert len(record["token_issued_at"]) == MAX_SESSION_TOKENS, (
        f"签发表应与 tokens 等长，实际 "
        f"{len(record['token_issued_at'])} vs {len(record['tokens'])}——"
        "两表漂移会让过期判据失准，且这是一份留痕"
    )


def test_expired_entries_are_pruned_on_issue(store):
    """过期的签发记录在签发时顺手清掉，不在 resolve 里做。"""
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    record["token_issued_at"]["sha256:stale"] = (
        time.time() - SESSION_TOKEN_TTL_SECONDS - 60
    )
    store._issue_token(record)
    assert "sha256:stale" not in record["token_issued_at"], (
        "过期的签发记录应在签发时清掉"
    )


def test_ttl_check_does_not_add_flushes(store):
    """TTL 判据不得在热路径引入新落盘。

    resolve 是每个请求都过的路径，而 last_used_at 刻意按
    LAST_USED_FLUSH_AFTER（一小时）降频——理由写在 auth.py 里：磁盘满或文件被
    锁住时，热路径上的写会把一枚有效令牌变成 500。

    负向锁：把 `_dirty` 那条分支去掉（或让 TTL 判据自己调 _flush），这条红。
    """
    token = _register(store)
    record = store._users[store.resolve(token).user_id]
    record["token_legacy_deadline"] = time.time() + LEGACY_TOKEN_GRACE_SECONDS

    flushes = []
    original = store._flush
    store._flush = lambda: (flushes.append(1), original())[1]

    for _ in range(30):# 连续 30 次命中，都不该触发落盘
        store.resolve(token)

    assert not flushes, (
        f"存量令牌宽限期内连续命中 30 次触发了 {len(flushes)} 次落盘——"
        "TTL 判据只读内存，节流由 _dirty 合并，不能每次鉴权都写盘"
    )


# ---------- 判据 4：撤销/改密仍然即时生效 ----------

def test_revoke_and_change_password_still_work(store):
    """加了 TTL 不得破坏既有的即时撤销语义。"""
    token = _register(store)
    store.revoke(token)
    assert store.resolve(token) is None, "登出必须立刻失效，不能等 TTL"

    token2 = _register(store, "u2")
    uid2 = store.resolve(token2).user_id
    # 签名是 (user_id, current_password, new_password, keep_token=None)：
    # 不传 keep_token 时除改密这一端外全端作废
    store.change_password(uid2, "correct-horse-battery", "new-battery-correct")
    assert store.resolve(token2) is None, "改密必须清掉全部令牌"


# ---------- 判据 5：用户可见接口不回显时间戳 ----------

def test_public_user_never_exposes_token_timestamps(store):
    """令牌时间戳不得出现在任何用户可见输出里——那是对存在性的泄露。"""
    token = _register(store)
    uid = store.resolve(token).user_id
    record = store._users[uid]
    # 新令牌有签发时刻，但**没有** legacy_deadline——那个字段只在存量令牌
    # 命中时写（见test_legacy_token_without_issued_at_still_resolves）。
    # 一枚新令牌不需要宽限期，它有自己的签发时刻。
    assert "token_issued_at" in record
    assert "token_legacy_deadline" not in record, (
        "新令牌不该被写上宽限期标记——它按签发时刻算TTL，用不上这层"
    )

    # _public_user 的形状在 auth_router 里，这里查它不会带出这些字段
    from app.core import auth_router
    import inspect
    src = inspect.getsource(auth_router._public_user)
    assert "token_issued_at" not in src
    assert "token_legacy_deadline" not in src
    assert "issued_at" not in src, (
        "_public_user 不许回显令牌签发时刻——那是令牌存在性的时间证据"
    )


def test_tokens_shape_unchanged_on_disk(store):
    """落盘形状：tokens 仍是字符串列表（存量库能原样读）。"""
    token = _register(store)
    store._flush()
    with open(store.path, encoding="utf-8") as f:
        data = json.load(f)
    record = list(data.values())[0]
    assert all(isinstance(t, str) for t in record["tokens"]), (
        "tokens 落盘形状不得变——改形状会让升级前的库读不出来"
    )
