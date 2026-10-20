# backend/tests/test_v0350_device_cookie.py
"""v0.35.0 #143②：Web 端续命凭据从 localStorage 搬进 httpOnly Cookie 的服务端契约。

这份文件只钉服务端那一半（浏览器/前端那一半在 test_v0340_web_device_vault.py）：

1. 签发收口：register / login / reauth 三处种 device_secret Cookie，属性逐条锁定
   （HttpOnly、Secure 默认开、SameSite=Strict、Path=/v1/auth/reauth、Max-Age=400 天）。
   adopt/renew 不种它——会话 Cookie 的签发门仍然只有 adopt 一扇。
2. 来源顺序：Cookie 优先，JSON body 回落（原生无 Cookie 通道、老 web 仍带 body）。
   两条同时出现时 Cookie 说了算；只要 Cookie 在，就必须带 X-CSRF。
3. CSRF：reauth 在公开名单里，中间件那道闸不经过它，所以端点自己按同一套
   X-CSRF 头与文案拒绝；body 里夹一把 secret 绕不过去。
4. 生命周期：每次 reauth 轮换 Cookie；logout / change-password / reset 清 Cookie
   且服务端秘密同时作废；400 天不活动 410 并清 Cookie；401 不擅自清（账号停用等
   可能是暂时的，客户端历史上也刻意保留那一格）。
5. 老客户端兼容：无 Cookie + JSON body 的 reauth 仍然成功，响应体里的 token /
   device_secret 字段原样保留（原生读的就是它）。
"""
import time

from app.core import authz
from app.core.auth import DEVICE_SECRET_INACTIVE_SECONDS, REAUTH_FAIL

PW = "correct-horse-battery"
ANSWERS = ["新市场小学", "hehai2024", "李建国"]

# 每条用例一个独立来源 IP：reauth 的两本限速账（按 IP / 按 secret）是进程级状态，
# 共用 127.0.0.1 会让"上一条用例刚把预算打满"变成下一条的 429（本文件里确实有
# 主动撞 401/410 的用例）。peer_client 让每条用例的来源可命名，桶就互不干扰。
_PEER = [0]


def _new_client():
    from tests.conftest import peer_client

    _PEER[0] += 1
    return peer_client((f"203.0.113.{_PEER[0]}", 40000 + _PEER[0]))


def _register(client, username="cookie-user"):
    res = client.post("/v1/auth/register",
                      json={"username": username, "password": PW,
                            "security_answers": ANSWERS})
    assert res.status_code == 200, res.text
    return res


def _set_cookie_headers(res):
    return [v for k, v in res.headers.multi_items() if k.lower() == "set-cookie"]


def _device_cookie_headers(res):
    return [v for v in _set_cookie_headers(res)
            if v.startswith(f"{authz.DEVICE_SECRET_COOKIE}=")]


def _one_device_cookie(res):
    hits = _device_cookie_headers(res)
    assert len(hits) == 1, f"应当恰好一枚 device_secret Set-Cookie，实际 {hits}"
    return hits[0]


def _record_of(store, token):
    principal = store.resolve(token)
    assert principal is not None
    return store._users[principal.user_id]


def _renew_devices(client, token):
    """把会话 Cookie 种上（logout/change-password 要它），返回会话令牌。"""
    res = client.post("/v1/auth/adopt",
                      headers={"Authorization": "Bearer " + token})
    assert res.status_code == 200, res.text
    return token


# ---------- 签发：register / login / reauth 三处，adopt/renew 不碰 ----------


def test_register_mints_the_device_cookie_with_locked_attributes(enforced, monkeypatch):
    """属性一条都不能少：HttpOnly（JS 读不到）、Secure 默认开、Strict、路径收口、400 天。"""
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)   # 默认必须是 Secure=on
    client = _new_client()
    res = _register(client)
    body = res.json()
    cookie = _one_device_cookie(res)
    assert cookie.startswith(f"{authz.DEVICE_SECRET_COOKIE}={body['device_secret']};"), \
        "Cookie 里的值与响应体不是同一把：老客户端与浏览器会各拿一把"
    low = cookie.lower()
    assert "httponly" in low, "没有 HttpOnly：XSS 能读走这把 400 天寿命的凭据"
    assert "secure" in low, "默认没开 Secure：现网 HTTPS 上这把钥匙会走明文信道"
    assert "samesite=strict" in low, "SameSite 不是 Strict：同源应用不需要为它留跨站口子"
    assert f"path={authz.DEVICE_SECRET_COOKIE_PATH}".lower() in low, \
        "Path 没收口到 reauth：一枚 400 天凭据会随每次业务请求出门"
    assert f"max-age={DEVICE_SECRET_INACTIVE_SECONDS}" in low, \
        "没有 Max-Age：浏览器一关就把静默重登丢了（与服务端 400 天口径不一致）"
    assert not [c for c in _set_cookie_headers(res)
                if c.startswith(f"{authz.SESSION_COOKIE}=")], \
        "注册响应种了会话 Cookie：adopt 的会话固定防线被绕开"


def test_secure_can_be_downgraded_only_explicitly(enforced, monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "0")
    client = _new_client()
    low = _one_device_cookie(_register(client)).lower()
    assert "secure" not in low, "显式降级没生效：本机 http 直连与测试会被 Secure Cookie 卡死"


def test_login_rotates_the_device_cookie_and_body_stays_compatible(enforced):
    client = _new_client()
    first = _register(client).json()
    res = client.post("/v1/auth/login",
                      json={"username": "cookie-user", "password": PW})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["device_secret"] != first["device_secret"], "登录没有发新的一把"
    cookie = _one_device_cookie(res)
    assert cookie.startswith(f"{authz.DEVICE_SECRET_COOKIE}={body['device_secret']};")
    assert "token" in body, "响应体契约被 Cookie 化改掉了：老客户端会当场失效"
    assert not [c for c in _set_cookie_headers(res)
                if c.startswith(f"{authz.SESSION_COOKIE}=")], \
        "登录响应种了会话 Cookie：adopt 仍是唯一的会话签发门"


def test_adopt_only_mints_the_session_cookie_and_never_touches_the_device_one(enforced):
    client = _new_client()
    body = _register(client).json()
    res = client.post("/v1/auth/adopt",
                      headers={"Authorization": "Bearer " + body["token"]})
    assert res.status_code == 200
    assert _device_cookie_headers(res) == [], \
        "adopt 顺手种了续命凭据 Cookie：签发门从三处漂成了第四处"


# ---------- 来源顺序：Cookie 优先，body 回落 ----------


def test_reauth_uses_the_cookie_and_rotates_it_on_every_call(enforced):
    client = _new_client()
    old = _register(client).json()["device_secret"]
    res = client.post("/v1/auth/reauth", headers={"X-CSRF": "1"})
    assert res.status_code == 200, res.text
    body = res.json()
    new = body["device_secret"]
    assert new and new != old, "reauth 没有轮换 secret：单次使用语义破了"
    cookie = _one_device_cookie(res)
    assert cookie.startswith(f"{authz.DEVICE_SECRET_COOKIE}={new};"), \
        "轮换后的值没写回 Cookie：浏览器手里还是那把已经被用掉的旧钥匙"
    assert body["token"], "reauth 没回 token：原生走 body 回落时换不到会话"
    # 旧值单次使用：清掉 Cookie 之后用 body 重放必须 401（不能因为浏览器现在还
    # 攥着新 Cookie 就把重放当成成功）。
    client.cookies.clear()
    replay = client.post("/v1/auth/reauth", json={"device_secret": old})
    assert replay.status_code == 401, "用过的 secret 还能再换一次会话"
    assert replay.json()["detail"] == REAUTH_FAIL


def test_the_body_fallback_still_serves_native_and_old_web(enforced):
    """没有 Cookie 的客户端（OkHttp / 老网页）走 JSON body，且不需要 X-CSRF。"""
    client = _new_client()
    secret = _register(client).json()["device_secret"]
    client.cookies.clear()
    res = client.post("/v1/auth/reauth", json={"device_secret": secret})
    assert res.status_code == 200, f"body 回落被 Cookie 化打掉了：{res.text}"
    assert res.json()["device_secret"] != secret
    assert _one_device_cookie(res), "body 成功路径没有把新值种回 Cookie"


def test_the_cookie_wins_when_both_sources_are_present(enforced):
    """Cookie 优先：body 里放一把错的值也换得动会话，证明服务端读的是 Cookie。"""
    client = _new_client()
    _register(client)
    res = client.post("/v1/auth/reauth",
                      headers={"X-CSRF": "1"},
                      json={"device_secret": "body-里-这把是错的"})
    assert res.status_code == 200, "既有 Cookie 又有 body 时没有按 Cookie 认"
    assert res.json()["device_secret"] != "body-里-这把是错的"


# ---------- CSRF：只认 X-CSRF，body 夹带绕不过去 ----------


def test_cookie_reauth_without_the_csrf_header_is_rejected(enforced):
    client = _new_client()
    secret = _register(client).json()["device_secret"]
    # 有 body（而且就是正确的那把）也救不了：Cookie 在，就必须声明 X-CSRF。
    blocked = client.post("/v1/auth/reauth", json={"device_secret": secret})
    assert blocked.status_code == 403, \
        "跨站请求拿着受害者的 Cookie 就能换会话：CSRF 第二道闸在 reauth 上没关门"
    assert authz.CSRF_DETAIL in blocked.text
    assert _device_cookie_headers(blocked) == [], "被 CSRF 挡下却轮换了 Cookie"
    assert "token" not in blocked.text
    ok = client.post("/v1/auth/reauth", headers={"X-CSRF": "1"})
    assert ok.status_code == 200, "同源声明过了还被挡：CSRF 闸门写反了"


def test_the_csrf_check_happens_before_the_rate_limit_ledger(enforced):
    """先 CSRF 后计费：跨站伪造的请求不该替攻击者消耗受害来源的失败预算。"""
    from app.core import auth_router

    client = _new_client()
    _register(client)
    before = sum(len(v) for v in auth_router._REAUTH_FAILS.values())
    assert client.post("/v1/auth/reauth").status_code == 403
    after = sum(len(v) for v in auth_router._REAUTH_FAILS.values())
    assert after == before, "CSRF 拒绝也记了一格失败账：预算被跨站请求白嫖"


# ---------- 生命周期：logout / 改密 / 找回清 Cookie ----------


def _assert_device_cookie_cleared(res):
    cookie = _one_device_cookie(res)
    low = cookie.lower()
    assert low.startswith(f"{authz.DEVICE_SECRET_COOKIE}=\"\";") or \
        low.startswith(f"{authz.DEVICE_SECRET_COOKIE}=;"), f"不是删除形状：{cookie}"
    assert "max-age=0" in low, f"没有 Max-Age=0：浏览器删不掉这枚 Cookie：{cookie}"
    assert f"path={authz.DEVICE_SECRET_COOKIE_PATH}".lower() in low


def test_logout_clears_the_device_cookie_and_the_server_side_secret(enforced):
    client = _new_client()
    body = _register(client).json()
    _renew_devices(client, body["token"])
    out = client.post("/v1/auth/logout", headers={"X-CSRF": "1"})
    assert out.status_code == 200
    _assert_device_cookie_cleared(out)
    # 服务端 revoke 连带清空 device_secrets：旧值（无论从 Cookie 还是 body 来）都失效。
    client.cookies.clear()
    res = client.post("/v1/auth/reauth", json={"device_secret": body["device_secret"]})
    assert res.status_code == 401, "退出之后那把 secret 还能换会话"


def test_password_change_clears_the_device_cookie(enforced):
    client = _new_client()
    body = _register(client).json()
    _renew_devices(client, body["token"])
    out = client.post("/v1/auth/change-password",
                      headers={"X-CSRF": "1"},
                      json={"current_password": PW,
                            "new_password": "brand-new-password"})
    assert out.status_code == 200, out.text
    _assert_device_cookie_cleared(out)
    client.cookies.clear()
    # 只测 secret 是否被服务端作废：用它打 reauth 应当是 401，不看响应体细节。
    res = client.post("/v1/auth/reauth", json={"device_secret": body["device_secret"]})
    assert res.status_code == 401, "改密之后旧 secret 还能静默重登"


def test_self_service_reset_clears_the_device_cookie(enforced):
    client = _new_client()
    old = _register(client).json()["device_secret"]
    out = client.post("/v1/auth/reset",
                      json={"username": "cookie-user", "answers": ANSWERS,
                            "new_password": "brand-new-password"})
    assert out.status_code == 200, out.text
    _assert_device_cookie_cleared(out)
    client.cookies.clear()
    res = client.post("/v1/auth/reauth", json={"device_secret": old})
    assert res.status_code == 401, "找回改密之后旧 secret 还能静默重登"


# ---------- 400 天：边界两侧与 401/410 的清/不清 ----------


def _age_secret(store, token, seconds_ago):
    record = _record_of(store, token)
    secret_entry = record["device_secrets"]
    assert len(secret_entry) == 1
    digest = next(iter(secret_entry))
    secret_entry[digest]["last_used_at"] = time.time() - seconds_ago
    return digest


def test_a_cookie_just_inside_the_400_day_limit_still_reauths(enforced):
    client = _new_client()
    body = _register(client).json()
    _age_secret(authz.auth_store, body["token"],
                DEVICE_SECRET_INACTIVE_SECONDS - 3600)
    res = client.post("/v1/auth/reauth", headers={"X-CSRF": "1"})
    assert res.status_code == 200, "399 天 23 小时没用过就被判死：400 天口径被改窄了"


def test_a_cookie_past_the_400_day_limit_is_410_and_is_cleared(enforced):
    client = _new_client()
    body = _register(client).json()
    digest = _age_secret(authz.auth_store, body["token"],
                         DEVICE_SECRET_INACTIVE_SECONDS + 3600)
    res = client.post("/v1/auth/reauth", headers={"X-CSRF": "1"})
    assert res.status_code == 410, f"太久没用应当 410，不是 {res.status_code}"
    _assert_device_cookie_cleared(res)
    record = _record_of(authz.auth_store, body["token"])
    assert digest not in record["device_secrets"], "过期后 secret 还留在表里"


def test_a_rejected_cookie_is_401_and_is_not_silently_cleared(enforced):
    """401 可能来自账号停用/服务端抖动；客户端历史上刻意保留这一格，服务端不替它删。"""
    client = _new_client()
    client.cookies.set(authz.DEVICE_SECRET_COOKIE, "not-a-real-secret",
                       path=authz.DEVICE_SECRET_COOKIE_PATH)
    res = client.post("/v1/auth/reauth", headers={"X-CSRF": "1"})
    assert res.status_code == 401
    assert res.json()["detail"] == REAUTH_FAIL
    assert _device_cookie_headers(res) == [], "401 就把 Cookie 清掉：一次抖动/停用就断了自愈路"
