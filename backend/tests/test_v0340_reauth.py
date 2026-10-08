# backend/tests/test_v0340_reauth.py
"""v0.34.0 判据 4/5/6/7/10：续命凭据与受众隔离。

这份文件守的是本版**最容易做错的一处**（PRD 第三节原话）：

- `device_secret` 在 `AuthStore.resolve()` 的查找路径里必须**根本不被查询**——
  不是"查到了也拒绝"，而是那条路径不存在。
- 反向同理：会话令牌打到 `/v1/auth/reauth` 也必须 401，不许"令牌也算凭据"。

两条各一把锁，而且都不是"看一眼返回值"那么轻：正向锁证明功能在（4 号判据），
反向锁证明两类凭据互相换不到对方的能力（5 号判据），生命周期锁证明"改密/退出/
封号"这三件事仍然**立刻、彻底**生效（6 号判据，PRD 第一节后半句）。

时间控制与 test_v0340_session_renew.py 同一套做法：改表里的时间戳，不 patch time。
"""
import ast
import inspect
import textwrap
import time

import pytest

from app.core.auth import (
    DEVICE_SECRET_INACTIVE_SECONDS,
    REAUTH_FAIL,
    SESSION_TOKEN_ABSOLUTE_CAP_SECONDS,
    AuthError,
    AuthStore,
    hash_secret,
    hash_token,
)

PW = "correct-horse-battery"


@pytest.fixture()
def store(tmp_path):
    return AuthStore(path=str(tmp_path / "users.json"))


def _login(store, name="u1", password=PW):
    """注册（注册即登录）并返回 (token, device_secret)。"""
    _principal, token, secret = store.register_for_new_session(username=name,
                                                               password=password)
    return token, secret


def _record_of(store, token):
    principal = store.resolve(token)
    assert principal is not None
    return store._users[principal.user_id]


def _expire_session(store, token):
    """让这枚会话令牌**到期**（绝对上限过了），而不是让它被吊销。

    这个区分是有内容的：退出（revoke）按判据 6 会连带清空 device_secrets，
    用它来"制造掉线"就把 reauth 要测的那把凭据一起擦掉了，测出来的 401 是假绿。
    真正需要静默重登的场景是"令牌自己到点"，这里就照那个形状改表。
    """
    record = _record_of(store, token)
    record["token_absolute_deadline"][hash_token(token)] = time.time() - 1
    assert store.resolve(token) is None, "改完表这枚令牌应当已经解不出来"
    return record


# ---------- 判据 4：reauth 恢复会话，绝对上限从这一刻重新起算 ----------


def test_reauth_issues_a_new_session_and_a_rotated_secret(store):
    """一句话验收的后半句的实现形状：一直用的人**不需要口令**就能回到账号上。

    变异：把 reauth 里 `record["device_secrets"].pop(digest)` 删掉（secret 不轮换），
    这条与下面那条单次使用的锁必须红。
    """
    token, secret = _login(store)
    _expire_session(store, token)

    principal, new_token, new_secret, payload = store.reauth(secret)
    assert principal is not None and new_token
    assert new_secret and new_secret != secret, "secret 没轮换：它能被重放"
    assert store.resolve(new_token) is not None
    assert payload["absolute_deadline"] > time.time(), "新会话的绝对上限从 reauth 起算"


def test_used_secret_is_single_use(store):
    """同一把 secret 第二次用必须 401——"用错一次就废"是它敢免会话凭据的前提。"""
    _token, secret = _login(store)
    _p, _t, _s, _payload = store.reauth(secret)
    with pytest.raises(AuthError) as exc:
        store.reauth(secret)
    assert exc.value.reason == REAUTH_FAIL
    assert exc.value.gone is False, "重放不是「过期」，不许报成 410 那句话"


def test_reauth_resets_the_absolute_cap_and_lets_the_old_session_die(store):
    """到第 181 天：老令牌已经解不出来，而一次 reauth 就把一切接回去。

    这条直接对应提案里的事故形状（"用得好的人被登出"）：绝对上限**应当**弄死老
    令牌，但用户无感知——因为客户端手里那把 secret 能换来新会话。
    """
    token, secret = _login(store)
    record = _record_of(store, token)
    digest = hash_token(token)
    cap = record["token_absolute_deadline"][digest]
    record["token_absolute_deadline"][digest] = time.time() - 1   # 模拟已过 180 天
    assert store.resolve(token) is None, "过了绝对上限的令牌必须失效"

    _p, new_token, _s, payload = store.reauth(secret)
    assert store.resolve(new_token) is not None
    assert payload["absolute_deadline"] >= time.time() + SESSION_TOKEN_ABSOLUTE_CAP_SECONDS - 5, \
        "新会话的绝对上限不是从 reauth 这一刻起算的"
    assert record["session_anchor"] > cap - SESSION_TOKEN_ABSOLUTE_CAP_SECONDS, \
        "锚点没挪：reauth 与 renew 混成了同一个动作"


# ---------- 判据 5：受众隔离（两条方向都要钉） ----------


def _string_constants_used_by(func) -> set:
    """函数体里真正被用到的字符串常量（docstring 除外）。

    为什么判"常量"而不是判"源码里有没有这个词"：本文件的 docstring 大量讨论
    device_secrets 这件事本身，纯文本扫描会把自己的说明判成违规；而一次
    `record.get("device_secrets")` 必然带一个字符串常量，加注释不会。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]              # 去掉 docstring
    found = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def test_resolve_never_consults_the_device_secrets_table():
    """结构级锁：鉴权热路径的**代码**里不许出现"查 secrets"这个动作。

    为什么不能只测行为：行为上"拿 secret 当 Bearer 被拒"可以靠一句显式拒绝实现，
    而那句拒绝一旦写成 `if digest in record["device_secrets"]: return None`，
    下一次有人把它改成 `return principal` 就没人拦得住——PRD 第三节要的是**那条
    路径不存在**。变异：往 resolve 或它的调用面里加一次 device_secrets 读取，
    这条必须红。
    """
    helpers = (AuthStore.resolve, AuthStore._find_record_by_token,
               AuthStore._token_expired, AuthStore._in_drain_window,
               AuthStore._token_seconds_left)
    for func in helpers:
        assert "device_secrets" not in _string_constants_used_by(func), \
            f"{func.__name__} 读取了 device_secrets：resolve 的调用面被打通了"


def test_a_secret_is_not_a_session_token(store):
    """把 device_secret 当会话令牌出示：解不出来。"""
    _token, secret = _login(store)
    assert store.resolve(secret) is None, "续命凭据换到了业务身份"


def test_a_session_token_is_not_a_secret(store):
    """反向半边：把会话令牌当 device_secret 提交给 reauth——401，不许"令牌也算凭据"。

    变异：在 reauth 里加一条"摘要在 tokens 表里也算命中"的兼容分支，这条必须红。
    """
    token, _secret = _login(store)
    with pytest.raises(AuthError) as exc:
        store.reauth(token)
    assert exc.value.reason == REAUTH_FAIL


@pytest.mark.parametrize("path", [
    "/v1/auth/me",
    "/v1/me/usage",
    "/v1/sessions",
])
def test_secret_as_bearer_is_401_on_business_endpoints(client, enforced, path):
    """逐条端点参数化的行为锁（PRD 判据 5 的原话是"逐条端点参数化"）。

    这里挑的三条覆盖三种形状：身份读取（/v1/auth/me）、本人数据面（/v1/me/usage）、
    以及会话列表（/v1/sessions，挂在 app.main 而不是某个 router 上——它是那种
    "改了 router 的名单却忘了顶层 app"最容易漏的一条）。

    401 必须来自中间件那句 UNAUTHORIZED_DETAIL，而不是来自某个端点恰好没查库。
    """
    from app.core.authz import UNAUTHORIZED_DETAIL

    enforced("真人")
    _token, secret = _login(_store_via_authz())
    res = client.get(path, headers={"Authorization": "Bearer " + secret})
    assert res.status_code == 401, f"{path} 用 secret 就放行了：{res.status_code}"
    assert res.json()["detail"] == UNAUTHORIZED_DETAIL, \
        f"{path} 的 401 不是中间件给的：受众隔离的实现点漂移了"


def _store_via_authz():
    """取 enforced 夹具换进去的那份身份库（路由读的就是它）。"""
    from app.core import authz

    return authz.auth_store


# ---------- 判据 6：改密 / 退出 / 封号连带吊销全部 secrets ----------


def test_password_change_wipes_every_device_secret(store):
    """改密的动机通常是"口令可能泄露"。留着任何一把 secret，自救就是假动作。"""
    _token, secret = _login(store, name="u1")
    # 同一账号的第二台设备：走 login（登录是"追加"，不清别人的令牌），它自己
    # 也会领到一把 secret——判据 6 要的是"改一次密，全账号的静默重登一起没"。
    other_token, _second_secret = store.login_for_new_session("u1", PW)[1:]
    record = _record_of(store, other_token)
    assert len(record["device_secrets"]) == 2
    store.change_password(record["user_id"], PW, "brand-new-password",
                          keep_token=other_token)
    assert record["device_secrets"] == {}, "改密之后还能静默重登"
    with pytest.raises(AuthError):
        store.reauth(secret)
    with pytest.raises(AuthError):
        store.reauth(_second_secret)


def test_self_service_reset_wipes_every_device_secret(store):
    """三题自助改密走的是同一句"口令可能泄露"，同一份清空动作。"""
    answers = ["新市场小学", "hehai2024", "李建国"]
    _token, secret = store.register_for_new_session(
        username="找回的人", password=PW, security_answers=answers)[1:]
    store.reset_password("找回的人", answers, "brand-new-password")
    with pytest.raises(AuthError) as exc:
        store.reauth(secret)
    assert exc.value.reason == REAUTH_FAIL


def test_admin_reset_wipes_device_secrets(store):
    _token, secret = _login(store)
    record = _record_of(store, _token)
    assert store.admin_reset_password(record["user_id"], "admin-set-password")
    with pytest.raises(AuthError):
        store.reauth(secret)


def test_disabling_an_account_kills_the_reauth_capability(store):
    """封号连 secret 一起清：否则"我停用了你"只是挡住业务面，会话一恢复就回来。

    只断言"当前 reauth 失败"是不够的——被 `_is_disabled` 的运行时检查挡住同样绿，
    而那条检查哪天挪走，secret 就悄悄活过来了。所以这里钉两件事：表当场是空的，
    以及**解封之后**那把老 secret 仍然打不开（判据 7：撤销必须不可逆）。
    """
    token, secret = _login(store)
    record = _record_of(store, token)
    store.disable_user(record["user_id"])
    assert record.get("device_secrets") in (None, {}), \
        "封号只加了个开关没清表：解封即复活"
    with pytest.raises(AuthError):
        store.reauth(secret)
    store.enable_user(record["user_id"])
    with pytest.raises(AuthError):
        store.reauth(secret)              # 解封后老 secret 依然打不开


def test_logout_wipes_device_secrets_and_takes_no_drain_window(store):
    """退出的两条半边：secrets 全清，且吊销**不走** 60 秒排水窗。"""
    token, secret = _login(store)
    record = _record_of(store, token)
    store.revoke(token)
    assert record["tokens"] == []
    assert record["device_secrets"] == {}, "退出之后那把 secret 还能换会话"
    assert record["token_revoked_at"] == {}, "退出在排水窗表里留了条目"
    with pytest.raises(AuthError):
        store.reauth(secret)


def test_rotate_token_wipes_secrets_and_starts_a_fresh_cap(store):
    """管理员强制全端重登：secrets 清空，而新签的那枚令牌**不能生下来就快到期**。

    后半句是真会发生的形状：如果不把 session_anchor 一起挪到新时刻，老锚点算出来的
    绝对上限可能只剩几秒——症状是"管理员点了轮换，当事人几秒后又被踢下线"。
    """
    token, _secret = _login(store)
    record = _record_of(store, token)
    record["session_anchor"] = time.time() - SESSION_TOKEN_ABSOLUTE_CAP_SECONDS + 30
    new_token = store.rotate_token(record["user_id"])
    assert record["device_secrets"] == {}
    principal = store.resolve(new_token)
    assert principal is not None, "轮换出来的令牌带着一个已经过去的绝对上限"
    assert principal.expires_in and principal.expires_in > 25 * 24 * 3600


# ---------- 判据 7：400 天不活动的 secret 走 410 ----------


def test_a_stale_secret_is_gone_not_merely_wrong(store):
    """连续 400 天没打开的人**必须**重新输口令——这是验收一句话的另一半。

    变异：把 DEVICE_SECRET_INACTIVE_SECONDS 的判据整条删掉（不活动期不判），
    这条必须红。
    """
    token, secret = _login(store)
    record = _record_of(store, token)
    digest = hash_secret(secret)
    record["device_secrets"][digest]["last_used_at"] = (
        time.time() - DEVICE_SECRET_INACTIVE_SECONDS - 3600)
    with pytest.raises(AuthError) as exc:
        store.reauth(secret)
    assert exc.value.gone is True, "过期与不对必须分两个状态码：410 与 401"
    assert digest not in record["device_secrets"], \
        "过期后 secret 还留在表里：重试就会把它变成「活着但被拒」"


def test_a_secret_just_inside_the_inactivity_limit_still_works(store):
    """边界另一侧：399 天 23 小时之前用过，仍然应当能换会话。

    只测"过期被拒"的锁可以靠"任何 secret 都被拒"蒙过——所以这条正向必须有。
    """
    token, secret = _login(store)
    record = _record_of(store, token)
    digest = hash_secret(secret)
    record["device_secrets"][digest]["last_used_at"] = (
        time.time() - DEVICE_SECRET_INACTIVE_SECONDS + 3600)
    _p, new_token, _s, _payload = store.reauth(secret)
    assert store.resolve(new_token) is not None


def test_reauth_count_is_carried_onto_the_rotated_secret(store):
    """reauth_count 是运维看得见的"这个人靠静默重登回来了几次"。

    这条同时钉住轮换的形状：新条目带着计数活下来，而不是整张表被写成一个布尔。
    """
    token, secret = _login(store)
    record = _record_of(store, token)
    _p, _t, secret2, _payload = store.reauth(secret)
    _p, _t, secret3, _payload = store.reauth(secret2)
    entry = record["device_secrets"][hash_secret(secret3)]
    assert entry["reauth_count"] == 2, f"计数没跟着轮换走：{entry}"


# ---------- 判据 10 的行为半边 + 响应契约 ----------


def test_reauth_endpoint_never_needs_a_session_credential(client, enforced):
    """免会话凭据可达：一个已经掉线的人，连 Authorization 头都没有。

    判据 10 的名单半边在 test_route_auth_contract.py（PUBLIC_PATHS 逐条点名），
    这一条钉的是"名单改了之后它确实还能被没登录的人用到"。
    """
    token, secret = _login(_store_via_authz())
    _expire_session(_store_via_authz(), token)
    res = client.post("/v1/auth/reauth", json={"device_secret": secret})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token"] and body["device_secret"] != secret
    assert "absolute_deadline" in body and "expires_at" in body


def test_reauth_endpoint_returns_410_for_a_stale_secret(client, enforced):
    store = _store_via_authz()
    token, secret = _login(store, name="久不打开的人")
    digest = hash_secret(secret)
    store._users[_record_of(store, token)["user_id"]]["device_secrets"][digest] \
        ["last_used_at"] = time.time() - DEVICE_SECRET_INACTIVE_SECONDS - 3600
    res = client.post("/v1/auth/reauth", json={"device_secret": secret})
    assert res.status_code == 410, f"太久没用应当 410，不是 {res.status_code}"


def test_reauth_endpoint_returns_401_for_a_session_token(client, enforced):
    store = _store_via_authz()
    token, _secret = _login(store, name="拿令牌来重登的人")
    res = client.post("/v1/auth/reauth", json={"device_secret": token})
    assert res.status_code == 401, "会话令牌在 reauth 这里也算凭据了"


def test_reauth_failures_are_rate_limited(client, enforced):
    """限速：reauth 是免会话凭据的端点，没有这本账它就是一个可以无限问的口子。

    两本账（来源 IP + 按 secret）任一本打满都 429，见 auth_router._reauth_throttled。
    """
    for i in range(12):
        res = client.post("/v1/auth/reauth", json={"device_secret": f"猜的一把-{i}"})
        if res.status_code == 429:
            break
    else:
        pytest.fail("连打 12 次错误 secret 都没触发 429：reauth 没有被限速")


def test_x_token_expires_in_is_on_authenticated_responses(client, enforced):
    """客户端判据 1 的数据来源：每个已鉴权响应都带着"这枚令牌还剩几秒"。

    这条也顺带钉住"不发"的那一半：bootstrap 管理员口令没有到期概念，头就不出现
    ——否则网页拿一个不存在的时间去判"要不要续期"，会出现永远不续或永远在续。
    """
    header = enforced("带头的用户")
    res = client.get("/v1/auth/me", headers=header)
    assert res.status_code == 200
    value = res.headers.get("X-Token-Expires-In")
    assert value is not None, "响应没带剩余秒数：客户端只能瞎猜什么时候该 renew"
    assert 0 < int(value) <= 30 * 24 * 3600, value

    admin = {"Authorization": "Bearer boot-token"}
    res = client.get("/v1/admin/users", headers=admin)
    assert res.status_code == 200
    assert "X-Token-Expires-In" not in res.headers, \
        "bootstrap 口令被报告成「有到期时间」：客户端会对着它触发 renew"


def test_x_token_expires_in_follows_the_absolute_cap_not_the_sliding_window(client,
                                                                           enforced):
    """剩余取的是**两道闸里更近的那个**。

    只报滑动期的话：绝对上限前 20 天客户端一直以为"还有二十九天"，从不 renew，
    到第 180 天直接 401——那正是本版要消灭的那一次掉线。
    """
    store = _store_via_authz()
    token, _secret = _login(store, name="快到头的人")
    header = {"Authorization": "Bearer " + token}
    record = store._users[store.resolve(token).user_id]
    record["token_absolute_deadline"][hash_token(token)] = time.time() + 3600
    res = client.get("/v1/auth/me", headers=header)
    assert int(res.headers["X-Token-Expires-In"]) <= 3600, \
        "响应报的是滑动期：绝对上限对客户端不可见，renew 永远不会发生"
