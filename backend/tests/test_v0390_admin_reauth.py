"""H-1：管理端高危写操作的 5 分钟二次验证票。"""

from app.core import admin_reauth, auth_router

BOOT = {"Authorization": "Bearer boot-token"}


def _reset():
    admin_reauth.reset_for_tests()
    auth_router.reset_admin_reauth_throttle_for_tests()


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

    同一管理员（BOOTSTRAP）跨多个 `cf-connecting-ip` 来源继续失败：失败数被摊到五个来源上，
    ①(user,IP) 那道每一格都够不着上限，只有**只按 user_id** 那道能把总预算吃满。
    摘掉那道 ⇒ 最后一发在全新 IP 上变 200，这条当场红（反例 i / iii 实测如此）。
    次数同样从钉里现取，不在用例里抄第二份数字。
    """
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    limit = auth_router._MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW
    _reset()
    ips = [f"203.0.113.{i}" for i in range(1, 6)]  # 五个不同来源
    for i in range(limit):
        h = {**BOOT, "CF-Connecting-IP": ips[i % len(ips)]}
        bad = client.post("/v1/auth/admin-reauth", json={"password": "wrong"}, headers=h)
        assert bad.status_code == 401
    # 一个"全新"的来源：(user,IP) 那道在这格是空的，只有 user 维度那道拦得住。
    fresh = {**BOOT, "CF-Connecting-IP": "198.51.100.7"}
    blocked = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers=fresh)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")


def test_admin_reauth_anon_identity_is_fail_closed():
    """S4：user_id 取不到（None/空）时这一道**不放松**——退化到单一 'anon' 格继续计。

    取不到身份绝不等于"这一道不判"：跳过 = 给能剥掉身份的来路留的口子（fail-open）。
    退化成共享的 'anon' 格才是 fail-closed。下面把上限这么多次失败**摊到两个来源**
    （每一格都够不着①号闸），第三个来源必须仍被同一格 anon 预算拦下；
    而真实 user_id 那格是空的，互不污染。次数同样取自常量，不抄第二份数字。
    """
    limit = auth_router._MAX_ADMIN_REAUTH_FAILURES_PER_WINDOW
    assert limit >= 2, "摊到两个来源要求各格都够不着上限，上限小于 2 时这一枚无从成立"
    _reset()
    first, rest = limit - limit // 2, limit // 2  # 两格各自 < 上限，合计 = 上限
    for ip, hits in (("203.0.113.1", first), ("203.0.113.2", rest)):
        assert hits < limit, "摊派写坏了：这一格自己就吃满了①号闸，anon 那格就没在单独承重"
        for _ in range(hits):
            assert not auth_router._admin_reauth_throttled(None, ip)
            auth_router._note_admin_reauth_failure(None, ip)
    # 无身份尝试跨来源仍共用一格 anon 预算 → 吃满后第三个来源也被拒。
    assert auth_router._admin_reauth_throttled(None, "198.51.100.9")
    # 且 anon 那一格不污染任何具名管理员：真 user_id 的预算仍是满的。
    assert not auth_router._admin_reauth_throttled("u_real_admin", "198.51.100.9")


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
