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
    """拿着管理员会话也不能无限试口令：10 次/10 分钟，超限 429 并带 Retry-After。"""
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    _reset()
    for _ in range(10):
        bad = client.post("/v1/auth/admin-reauth", json={"password": "wrong"},
                          headers=BOOT)
        assert bad.status_code == 401
    blocked = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers=BOOT)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")
    auth_router.reset_admin_reauth_throttle_for_tests()
    allowed = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                          headers=BOOT)
    assert allowed.status_code == 200


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
