"""H-1：管理端高危写操作的 5 分钟二次验证票。"""

from app.core import admin_reauth, auth_router

BOOT = {"Authorization": "Bearer boot-token"}


def _reset():
    admin_reauth.reset_for_tests()
    auth_router.reset_admin_reauth_throttle_for_tests()


def _src_ip(n: int) -> str:
    """第 n 次尝试换一个**互不相同**的来源（RFC5737 测试网段，两段子网够用到几百格）。

    用例靠它把失败摊到不同 IP 上，让 (user,IP) 那道每格只留一笔、够不着自己的上限，
    于是"拦得住的那道"只能是只按 user_id 那道——来源数由上限常量决定，所以这里留足余量。
    """
    block, host = divmod(n, 200)
    return f"{'203.0.113' if block == 0 else '198.51.100'}.{host + 1}"


def test_high_risk_admin_write_needs_a_ticket(client, enforced, monkeypatch):
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    _reset()
    blocked = client.post("/v1/admin/config", json={"registration_open": False},
                          headers=BOOT)
    assert blocked.status_code == 428
    assert "二次验证" in blocked.json()["detail"]

    wrong = client.post("/v1/auth/admin-reauth", json={"password": "not-the-token"},
                        headers=BOOT)
    assert wrong.status_code == 401

    ok = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                     headers=BOOT)
    assert ok.status_code == 200
    cookie = ok.headers.get("set-cookie", "")
    assert admin_reauth.COOKIE_NAME + "=" in cookie
    assert "HttpOnly" in cookie and "Path=/v1;" in cookie

    allowed = client.post("/v1/admin/config", json={"registration_open": False},
                          headers=BOOT)
    assert allowed.status_code == 200, allowed.text


def test_reauth_gate_covers_admin_writes_outside_admin_prefix(client, enforced, monkeypatch):
    """H-1 只按 /v1/admin/ 前缀拦会漏掉 provider/search-key/memory-decay 写口。"""
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    _reset()
    provider = {"label": "t", "base_url": "https://example.com/v1",
                "model": "m", "api_key": "test-key"}
    probes = [
        ("POST", "/v1/providers", provider),
        ("POST", "/v1/providers/p_missing/default", None),
        ("POST", "/v1/providers/p_missing/test", None),
        ("POST", "/v1/providers/test", provider),
        ("PUT", "/v1/search-keys/bocha", {"api_key": "test-key"}),
        ("DELETE", "/v1/search-keys/bocha", None),
        ("POST", "/v1/memory/decay?decay_factor=0.9", None),
    ]
    for method, path, body in probes:
        res = client.request(method, path, json=body, headers=BOOT)
        assert res.status_code == 428, (method, path, res.status_code, res.text)

    ok = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                     headers=BOOT)
    assert ok.status_code == 200
    assert "Path=/v1;" in ok.headers.get("set-cookie", "")
    # 票已换到 Cookie；重试其中一个前缀外写口，必须不再被 428 拦。
    retried = client.put("/v1/search-keys/bocha", json={"api_key": "test-key"},
                         headers=BOOT)
    assert retried.status_code != 428, retried.text


def test_admin_reauth_is_rate_limited(client, enforced, monkeypatch):
    """拿着管理员会话也不能在同一来源上无限试口令：①(user,IP) 那道的判据。

    这条**只钉 (user,IP) 那一道**：把同一来源打到上限之后先把"只按 user_id 那道"的账
    清掉，于是下面这一发 429 只能由 (user,IP) 那格撑住——把它摘掉，这条立刻红（反例 iv），
    证明这一单没有把旧牙磨掉。跨来源总预算由 `..._budget_follows_the_user_across_ips` 单独钉。

    次数一律从 `_MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW` 现取，不在用例里抄第二份数字：
    那一格是这套上限的唯一出处（本仓规矩"数只活在钉里"），抄成字面量就成了会腐烂的副本。
    """
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    limit = auth_router._MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW
    _reset()
    for _ in range(limit):
        bad = client.post("/v1/auth/admin-reauth", json={"password": "wrong"},
                          headers=BOOT)
        assert bad.status_code == 401
    # 只清 user-only 那道，保留 (user,IP) 那道：这一发的 429 必须来自旧那一道。
    auth_router.reset_admin_reauth_by_user_for_tests()
    blocked = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers=BOOT)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")
    auth_router.reset_admin_reauth_throttle_for_tests()
    allowed = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers=BOOT)
    assert allowed.status_code == 200


def test_admin_reauth_budget_follows_the_user_across_ips(client, enforced, monkeypatch):
    """S4：注释承诺"换 IP 的人对同一账号仍受同一格预算"——这一枚把它变成真的。

    同一管理员（BOOTSTRAP）每次换一个 `cf-connecting-ip` 来源继续失败，把总账打到
    ②号那道自己的上限：①(user,IP) 那道每格只有一笔、永远够不着，只有**只按 user_id**
    那道能把跨来源的总预算吃满。摘掉那道 ⇒ 最后一发在全新来源上变 200，这条当场红
    （反例 iii）。把这一道指回①的上限（反例 i）由 `..._uses_its_own_constant` 那枚负责红。
    次数从 `_MAX_ADMIN_REAUTH_USER_FAILURES_PER_WINDOW` 现取，不在用例里抄第二份数字。
    """
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    limit = auth_router._MAX_ADMIN_REAUTH_USER_FAILURES_PER_WINDOW
    _reset()
    for n in range(limit):            # 每来源一笔：①号那道每格都只有一笔
        h = {**BOOT, "CF-Connecting-IP": _src_ip(n)}
        bad = client.post("/v1/auth/admin-reauth", json={"password": "wrong"}, headers=h)
        assert bad.status_code == 401, (n, bad.status_code, bad.text)
    # 一个"全新"的来源：(user,IP) 那道在这格是空的，只有 user 维度那道拦得住。
    fresh = {**BOOT, "CF-Connecting-IP": _src_ip(limit)}
    blocked = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers=fresh)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")


def test_admin_reauth_user_gate_uses_its_own_constant(client, enforced, monkeypatch):
    """②号那道吃自己的独立上限常量，形状必须是"到①的上限不拦、到②的上限才拦"。

    同一管理员每次换一个来源失败（①号那道每格只有一笔，永远够不着自己那格的上限）：
    - 总账刚到①号上限时，全新来源**必须还放得过去**——把②指回
      `_MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW`（或两道的值改回相同），这一截当场红（反例 i）；
    - 继续打到②号上限才 429——把这一道的阈值改成极大（等于取消这一道），这一截当场红（反例 ii）。
    次数一律从两个常量现取，用例里不抄第二份数字。
    """
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    ip_limit = auth_router._MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW
    user_limit = auth_router._MAX_ADMIN_REAUTH_USER_FAILURES_PER_WINDOW
    assert user_limit > ip_limit, "②号那道必须有自己更松的上限，否则独立常量是挂着看的"
    _reset()
    for n in range(ip_limit):
        bad = client.post("/v1/auth/admin-reauth", json={"password": "wrong"},
                          headers={**BOOT, "CF-Connecting-IP": _src_ip(n)})
        assert bad.status_code == 401, (n, bad.status_code, bad.text)
    loose = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                        headers={**BOOT, "CF-Connecting-IP": _src_ip(user_limit)})
    assert loose.status_code == 200, f"②号那道被并回①的上限了：{loose.status_code} {loose.text}"
    for n in range(ip_limit, user_limit):
        bad = client.post("/v1/auth/admin-reauth", json={"password": "wrong"},
                          headers={**BOOT, "CF-Connecting-IP": _src_ip(n)})
        assert bad.status_code == 401, (n, bad.status_code, bad.text)
    blocked = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers={**BOOT, "CF-Connecting-IP": _src_ip(user_limit + 1)})
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")


def test_admin_reauth_anon_identity_is_fail_closed():
    """S4：user_id 取不到（None/空）时②号那道**不放松**——塌成单一 'anon' 格继续计。

    取不到身份绝不等于"这一道不判"：跳过 = 给能剥掉身份的来路留的口子（fail-open）。
    退化成共享的 'anon' 格才是 fail-closed。这里每来源只打一笔，①号那道每格都够不着
    自己那格的上限（①退化后是 `anon|<IP>`，**不**塌成单一格，两道退化到的形状不同），
    于是吃满的只可能是②号那道。两截都钉：
    总账到①号上限时还不拦（把②指回①的常量就红，反例 i），到②号上限才拦（阈值改成
    极大就红，反例 ii）。次数从常量现取，不抄第二份数字。
    """
    ip_limit = auth_router._MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW
    user_limit = auth_router._MAX_ADMIN_REAUTH_USER_FAILURES_PER_WINDOW
    assert user_limit > ip_limit >= 2, "两道上限分开钉且②更松；否则这两截无从成立"
    _reset()
    for n in range(ip_limit):        # 每来源一笔：①号那道每格始终只有 1 笔
        assert not auth_router._admin_reauth_throttled(None, _src_ip(n))
        auth_router._note_admin_reauth_failure(None, _src_ip(n))
    # 只到①号那道的上量：②号那道有自己的上限，这时候还不该拦。
    assert not auth_router._admin_reauth_throttled(None, _src_ip(user_limit))
    for n in range(ip_limit, user_limit):
        auth_router._note_admin_reauth_failure(None, _src_ip(n))
    # 无身份尝试跨来源共用②那一格 anon 预算 → 吃满后新来源也被拒。
    assert auth_router._admin_reauth_throttled(None, _src_ip(user_limit))
    # 且 anon 那一格不污染任何具名管理员：真 user_id 那一格的预算仍完整、未被消耗。
    assert not auth_router._admin_reauth_throttled("u_real_admin", _src_ip(user_limit))


def test_admin_only_unsafe_routes_outside_admin_prefix_require_reauth():
    """注册表锁：前缀外的管理员写口必须挂 require_admin_reauth，漏接线就红。"""
    from fastapi.routing import APIRoute
    from app.core import authz
    from app.main import app
    from tests.test_route_auth_contract import _callables_of

    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = set(getattr(route, "methods", set())) & set(authz._UNSAFE_METHODS)
        if not methods or route.path.startswith("/v1/admin/"):
            continue
        calls = {getattr(c, "__name__", str(c)) for c in _callables_of(route.dependant)}
        if "require_admin" in calls and "require_admin_reauth" not in calls:
            offenders.append((route.path, sorted(methods)))
    assert not offenders, f"前缀外管理员写口缺二次验证依赖：{offenders}"


def test_web_app_handles_428_with_an_in_page_password_input():
    """网页版的管理员写口也会收到 428；必须页内输入并重试一次，不能弹原生 prompt。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    api = (root / "backend" / "app" / "web" / "static" / "api.js").read_text(encoding="utf-8")
    html = (root / "backend" / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "res.status === 428" in api
    assert "askAdminReauth" in api and "window.prompt(" not in api
    assert 'id="reauthModal"' in html and 'id="reauthPw" type="password"' in html
    assert '"/v1/auth/admin-reauth"' in api


def test_security_log_details_include_reauth_events():
    from app.core import security_log

    assert "reauth.required" in security_log.DETAILS
    assert "admin.reauth.denied" in security_log.DETAILS


def test_a_plain_user_cannot_get_an_admin_ticket(client, enforced, monkeypatch):
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    _reset()
    user = enforced("普通用户")
    res = client.post("/v1/auth/admin-reauth", json={"password": "whatever"},
                      headers=user)
    assert res.status_code == 403


def test_ticket_is_bound_to_the_user_and_expires(monkeypatch):
    _reset()
    token = admin_reauth.issue("u_admin")
    assert admin_reauth.valid(token, "u_admin")
    assert not admin_reauth.valid(token, "u_other")
    # 过期：把 monotonic 拨到 TTL 之后
    now = admin_reauth.time.monotonic()
    monkeypatch.setattr(admin_reauth.time, "monotonic",
                        lambda: now + admin_reauth.TICKET_TTL_SECONDS + 1)
    assert not admin_reauth.valid(token, "u_admin")


def test_admin_ui_uses_in_page_reauth_input_not_window_prompt():
    """MIUI 浏览器的 window.prompt 输入框实测只收 15 个字符，长口令会被无声截断。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    js = (root / "backend" / "app" / "web" / "admin" / "admin.js").read_text(encoding="utf-8")
    html = (root / "backend" / "app" / "web" / "admin" / "index.html").read_text(encoding="utf-8")
    css = (root / "backend" / "app" / "web" / "admin" / "admin.css").read_text(encoding="utf-8")
    assert "window.prompt(" not in js
    assert "askAdminPassword" in js and "await askAdminPassword()" in js
    assert 'id="reauthPw" type="password"' in html
    assert 'id="reauth"' in html and 'id="reauthOk"' in html
    assert "#reauth input" in css
    assert "/v1/auth/admin-reauth" in js


def test_offline_tool_path_can_create_a_real_admin(tmp_path):
    from app.core.auth import AuthStore, AuthError

    store = AuthStore(path=str(tmp_path / "users.json"))
    record = store.admin_create_user("ops-admin", "correct-horse-battery",
                                     role="admin")
    assert record["role"] == "admin"
    assert store.verify_password(record["user_id"], "correct-horse-battery")
    assert not store.verify_password(record["user_id"], "wrong-password")
    principal, _token = store.login("ops-admin", "correct-horse-battery")
    assert principal.role == "admin"
    try:
        store.admin_create_user("bad-role", "correct-horse-battery", role="owner")
    except AuthError:
        pass
    else:
        raise AssertionError("非法角色没有被拒")
